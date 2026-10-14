import json

import app


def test_config_has_fixed_options():
    client = app.app.test_client()
    data = client.get("/api/config").get_json()
    assert len(data["prompts"]) == 4
    assert [item["name"] for item in json.loads((app.ROOT / "tools.json").read_text())] == ["python", "google_search"]


def test_default_base_url_has_no_trailing_slash():
    assert not app.OPENAI_BASE_URL.endswith("/")


def test_prompt_change_resets_context(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "test.db")
    app.init_db()
    client = app.app.test_client()
    session_id = client.post("/api/sessions", json={"model": app.MODELS[0], "prompt_key": "tutor"}).get_json()["id"]
    with app.db() as connection:
        connection.execute("INSERT INTO messages(session_id,role,content) VALUES(?,?,?)", (session_id, "user", "hello"))
    result = client.patch(f"/api/sessions/{session_id}", json={"prompt_key": "reviewer"}).get_json()
    assert result == {"ok": True, "context_reset": True}
    assert client.get(f"/api/sessions/{session_id}").get_json()["session"]["context_start"] == 1
