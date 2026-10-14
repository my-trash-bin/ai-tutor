"""Persistent Python worker. This file only runs in an isolated container."""
import contextlib
import io
import json
import os
import sys

from IPython.core.interactiveshell import InteractiveShell
from traitlets.config import Config

LIMIT = 24_000


class LimitedText(io.StringIO):
    def __init__(self):
        super().__init__()
        self.remaining = LIMIT
        self.truncated = False

    def write(self, text):
        length = len(text)
        self.truncated |= length > self.remaining
        super().write(text[: self.remaining])
        self.remaining = max(0, self.remaining - length)
        return length


def main():
    protocol = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    config = Config()
    config.HistoryManager.enabled = False
    config.InteractiveShell.colors = "nocolor"
    config.InteractiveShell.xmode = "Plain"
    config.InteractiveShell.ast_node_interactivity = "last_expr"
    shell = InteractiveShell(config=config)
    shell.displayhook.write_output_prompt = lambda: None
    protocol.write('{"ready":true}\n')
    for line in sys.stdin:
        output = LimitedText()
        try:
            code = json.loads(line)["code"]
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = shell.run_cell(code, store_history=False)
            reply = {"ok": result.success, "output": output.getvalue(), "truncated": output.truncated}
        except BaseException as exc:
            reply = {"ok": False, "output": f"{type(exc).__name__}: {exc}"[:LIMIT], "truncated": False}
        protocol.write(json.dumps(reply, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()

