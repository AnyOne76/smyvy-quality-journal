# -*- coding: utf-8 -*-
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app_server


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_json(method, url, data=None, headers=None):
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), resp.headers
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8")
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"raw": payload}
        return e.code, parsed, e.headers


def test_password_hash_roundtrip():
    stored = app_server.hash_password("secret-pass")
    assert stored.startswith("pbkdf2$")
    assert app_server.verify_password("secret-pass", stored)
    assert not app_server.verify_password("wrong", stored)


def test_init_schema_creates_users_table(tmp_path, monkeypatch):
    db_file = tmp_path / "smyvy_test.db"
    monkeypatch.setattr(app_server, "DB_PATH", str(db_file))
    app_server.init_schema()
    con = app_server.db()
    tables = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    con.close()
    assert "users" in tables
    assert "probes" in tables


def test_api_login_and_me(tmp_path, monkeypatch):
    db_file = tmp_path / "smyvy_http.db"
    monkeypatch.setattr(app_server, "DB_PATH", str(db_file))
    monkeypatch.setattr(app_server, "SECRET", b"0" * 32)
    app_server.init_schema()
    pw_hash = app_server.hash_password("testpass")
    con = app_server.db()
    con.execute(
        "INSERT INTO users(login, pass_hash, role, active, created_at, full_name, must_change_pw) "
        "VALUES(?,?,?,?,?,?,?)",
        ("tester@example.com", pw_hash, "quality", 1, app_server.now(), "Тест", 0),
    )
    con.commit()
    con.close()

    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), app_server.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = "http://127.0.0.1:%d" % port
        code, body, _ = _http_json("GET", base + "/api/journal")
        assert code == 401

        code, body, headers = _http_json(
            "POST",
            base + "/api/login",
            {"login": "tester@example.com", "password": "testpass"},
        )
        assert code == 200
        assert body.get("login") == "tester@example.com"
        cookie = headers.get("Set-Cookie") or headers.get("set-cookie")
        assert cookie and "sid=" in cookie
        sid = cookie.split("sid=", 1)[1].split(";", 1)[0]

        code, me, _ = _http_json(
            "GET",
            base + "/api/me",
            headers={"Cookie": "sid=%s" % sid},
        )
        assert code == 200
        assert me.get("login") == "tester@example.com"
        assert me.get("role") == "quality"
    finally:
        srv.shutdown()
