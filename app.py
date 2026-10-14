from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request
import httpx

from prompts import PROMPTS

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "chat.db"
TOOLS = [{"type": "function", **item, "strict": False} for item in json.loads((ROOT / "tools.json").read_text())]
MODELS = [x.strip() for x in os.environ.get("OPENAI_MODELS", "gpt-5-mini").split(",") if x.strip()]
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
MAX_ROUNDS = 12
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
workers: dict[str, "DockerPython"] = {}
workers_lock = threading.Lock()


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
          prompt_key TEXT NOT NULL, context_start INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
          role TEXT NOT NULL, content TEXT NOT NULL, attachments TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        """)


class DockerPython:
    def __init__(self, session_id: str):
        self.name = "neito-" + session_id
        self.process = None
        self.lock = threading.Lock()

    def receive(self, timeout=30):
        data, deadline = bytearray(), time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in data:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Python tool timed out.")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Python worker stopped.")
                data.extend(chunk)
                if len(data) > 512_000:
                    raise RuntimeError("Python output was too large.")
        return json.loads(data.decode())

    def execute(self, code):
        with self.lock:
            if self.process is None:
                self.process = subprocess.Popen([
                    "docker", "run", "--rm", "-i", "--init", "--pull=never",
                    "--name", self.name, "--network=none", "--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges", "--memory=512m", "--cpus=1",
                    "--pids-limit=64", "--user=65534:65534",
                    "--tmpfs=/tmp:rw,nosuid,nodev,size=128m,mode=1777", "neito-python:local",
                ], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                if self.receive() != {"ready": True}:
                    raise RuntimeError("Python worker did not start.")
            self.process.stdin.write((json.dumps({"code": code}) + "\n").encode())
            self.process.stdin.flush()
            return self.receive()

    def close(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=10, check=False)
        if self.process and self.process.poll() is None:
            self.process.kill()


def python_worker(session_id):
    with workers_lock:
        return workers.setdefault(session_id, DockerPython(session_id))


def search(queries):
    key = os.environ.get("SERPER_API_KEY", "")
    if not key:
        return {"error": "SERPER_API_KEY is not configured."}
    results = []
    with httpx.Client(trust_env=False) as client:
        for query in queries[:8]:
            try:
                response = client.post("https://google.serper.dev/search", headers={"X-API-KEY": key}, json={"q": query, "num": 5}, timeout=20)
                response.raise_for_status()
                body = response.json()
                results.append({"query": query, "results": [{k: x.get(k, "") for k in ("title", "link", "snippet")} for x in body.get("organic", [])[:5]]})
            except (httpx.HTTPError, ValueError):
                results.append({"query": query, "error": "Search failed."})
    return results


def dispatch(session_id, name, raw_arguments):
    try:
        arguments = json.loads(raw_arguments)
        if name == "python" and isinstance(arguments.get("code"), str):
            result = python_worker(session_id).execute(arguments["code"][:32_000])
        elif name == "google_search" and isinstance(arguments.get("queries"), list):
            result = search([q for q in arguments["queries"] if isinstance(q, str) and q.strip()])
        else:
            result = {"error": "Invalid tool arguments."}
    except (ValueError, TypeError, OSError, RuntimeError, TimeoutError) as exc:
        result = {"error": str(exc)}
    return json.dumps(result, ensure_ascii=False)[:80_000]


def ask_openai(session, history):
    key = os.environ.get("OPENAI_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = history
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=120) as client:
        for round_number in range(MAX_ROUNDS + 1):
            response = client.post(f"{OPENAI_BASE_URL}/responses", headers=headers, json={
                "model": session["model"], "instructions": PROMPTS[session["prompt_key"]][1],
                "input": payload, "tools": TOOLS, "parallel_tool_calls": False,
                "store": False, "tool_choice": "none" if round_number == MAX_ROUNDS else "auto",
                "include": ["reasoning.encrypted_content"],
            })
            response.raise_for_status()
            items = response.json().get("output", [])
            payload.extend(items)
            calls = [item for item in items if item.get("type") == "function_call"]
            if not calls:
                return "\n".join(part.get("text", part.get("refusal", "")) for item in items if item.get("type") == "message" for part in item.get("content", [])) or "[응답 내용이 없습니다.]"
            for call in calls:
                payload.append({"type": "function_call_output", "call_id": call["call_id"], "output": dispatch(session["id"], call["name"], call["arguments"])})
    raise RuntimeError("Tool call limit exceeded.")


def read_text_files(files):
    attachments, total = [], 0
    for file in files:
        if not file.filename:
            continue
        raw = file.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("Each text file must be 1 MB or smaller.")
        total += len(raw)
        if total > 4_194_304:
            raise ValueError("Attached text files must be 4 MB or smaller in total.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{file.filename} is not a UTF-8 text file.") from exc
        attachments.append({"name": file.filename, "content": text})
    return attachments


def inline_user_content(content, attachments):
    chunks = [content] if content else []
    for item in attachments:
        filename = json.dumps(item["name"], ensure_ascii=False)
        chunks.append(f'<attachment filename={filename}>\n{item["content"]}\n</attachment>')
    return "\n\n".join(chunks)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/config")
def config():
    return jsonify({"models": MODELS, "prompts": [{"key": key, "name": value[0]} for key, value in PROMPTS.items()]})


@app.get("/api/sessions")
def list_sessions():
    with db() as connection:
        return jsonify([dict(row) for row in connection.execute("SELECT * FROM sessions ORDER BY updated_at DESC")])


@app.post("/api/sessions")
def create_session():
    body, session_id = request.get_json(silent=True) or {}, uuid.uuid4().hex[:12]
    model, prompt_key = body.get("model", MODELS[0]), body.get("prompt_key", "tutor")
    if model not in MODELS or prompt_key not in PROMPTS:
        return jsonify(error="Invalid model or prompt."), 400
    with db() as connection:
        connection.execute("INSERT INTO sessions(id,title,model,prompt_key) VALUES(?,?,?,?)", (session_id, "새 채팅", model, prompt_key))
    return jsonify(id=session_id), 201


@app.get("/api/sessions/<session_id>")
def get_session(session_id):
    with db() as connection:
        session = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            return jsonify(error="Session not found."), 404
        messages = [dict(row) for row in connection.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,))]
        for message in messages:
            message["attachments"] = [{"name": item["name"]} for item in json.loads(message["attachments"])]
        return jsonify(session=dict(session), messages=messages)


@app.patch("/api/sessions/<session_id>")
def update_session(session_id):
    body = request.get_json(silent=True) or {}
    with db() as connection:
        session = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            return jsonify(error="Session not found."), 404
        model, prompt_key = body.get("model", session["model"]), body.get("prompt_key", session["prompt_key"])
        if model not in MODELS or prompt_key not in PROMPTS:
            return jsonify(error="Invalid model or prompt."), 400
        context_start = session["context_start"]
        if prompt_key != session["prompt_key"]:
            context_start = connection.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)).fetchone()[0]
        connection.execute("UPDATE sessions SET model=?,prompt_key=?,context_start=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (model, prompt_key, context_start, session_id))
    if prompt_key != session["prompt_key"]:
        with workers_lock:
            worker = workers.pop(session_id, None)
        if worker:
            worker.close()
    return jsonify(ok=True, context_reset=prompt_key != session["prompt_key"])


@app.post("/api/sessions/<session_id>/messages")
def send_message(session_id):
    content = request.form.get("content", "").strip()
    files = request.files.getlist("files")
    if not content and not files:
        return jsonify(error="Message or file is required."), 400
    try:
        attachments = read_text_files(files)
        with db() as connection:
            session = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session:
                return jsonify(error="Session not found."), 404
            connection.execute("INSERT INTO messages(session_id,role,content,attachments) VALUES(?,?,?,?)", (session_id, "user", content, json.dumps(attachments, ensure_ascii=False)))
            if connection.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)).fetchone()[0] == 1:
                connection.execute("UPDATE sessions SET title=? WHERE id=?", ((content or attachments[0]["name"])[:40], session_id))
            connection.execute("UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
            rows = connection.execute("SELECT role,content,attachments FROM messages WHERE session_id=? ORDER BY id LIMIT -1 OFFSET ?", (session_id, session["context_start"])).fetchall()
        history = []
        for row in rows:
            if row["role"] == "assistant":
                history.append({"role": "assistant", "content": row["content"]})
                continue
            text = inline_user_content(row["content"], json.loads(row["attachments"]))
            history.append({"role": "user", "content": text})
        answer = ask_openai(session, history)
        with db() as connection:
            connection.execute("INSERT INTO messages(session_id,role,content) VALUES(?,?,?)", (session_id, "assistant", answer))
            connection.execute("UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
        return jsonify(content=answer, attachments=[{"name": item["name"]} for item in attachments])
    except httpx.HTTPStatusError as exc:
        return jsonify(error=f"OpenAI API error ({exc.response.status_code})."), 502
    except (httpx.RequestError, ValueError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 502


@atexit.register
def shutdown():
    for worker in workers.values():
        worker.close()


init_db()
