from starlette.testclient import TestClient

from herdrhq.app import create_app
from herdrhq.config import Config


def test_state_shape_no_hosts():
    app = create_app(Config(auth_enabled=False))
    client = TestClient(app, base_url="http://localhost")
    r = client.get("/api/state")
    assert r.status_code == 200
    data = r.json()
    assert set(data) >= {"generated_at", "poll_interval", "terminal", "hosts"}
    assert data["terminal"] == {"enabled": True, "input": True}
    assert data["hosts"] == []


def test_refresh():
    app = create_app(Config(auth_enabled=False))
    client = TestClient(app, base_url="http://localhost")
    assert client.post("/api/refresh").json() == {"ok": True}


def test_term_stream_requires_params():
    app = create_app(Config(auth_enabled=False))
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/api/term/stream").status_code == 400
    r = client.get("/api/term/stream", params={"host": "nope", "pane": "w:p1"})
    assert r.status_code == 400
    assert "unknown host" in r.json()["error"]


def test_term_input_validation():
    app = create_app(Config(auth_enabled=False))
    client = TestClient(app, base_url="http://localhost")
    r = client.post("/api/term/input", json={"host": "x", "pane": "y", "ops": "no"})
    assert r.status_code == 400
    r = client.post("/api/term/input", content=b"not json")
    assert r.status_code == 400


def test_term_input_disabled():
    app = create_app(Config(auth_enabled=False, terminal_input=False))
    client = TestClient(app, base_url="http://localhost")
    r = client.post("/api/term/input", json={"host": "x", "pane": "y", "ops": []})
    assert r.status_code == 403


def test_static_and_index():
    app = create_app(Config(auth_enabled=False))
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/").status_code == 200
    assert "herdr" in client.get("/").text
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_auth_blocks_everything():
    app = create_app(Config(), secret="sekrit")
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/api/state").status_code == 401
    r = client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 401
    assert "sign in" in r.text


def test_auth_bearer_and_query_token():
    app = create_app(Config(), secret="sekrit")
    client = TestClient(app, base_url="http://localhost")
    r = client.get("/api/state", headers={"authorization": "Bearer sekrit"})
    assert r.status_code == 200
    r = client.get("/api/state", params={"token": "sekrit"})
    assert r.status_code == 200
    r = client.get("/api/state", params={"token": "wrong"})
    assert r.status_code == 401


def test_auth_login_form_sets_cookie():
    app = create_app(Config(), secret="sekrit")
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    r = client.post("/auth", data={"token": "sekrit", "next": "/"})
    assert r.status_code == 303
    assert "herdrhq_auth" in r.headers.get("set-cookie", "")
    fresh = TestClient(app, base_url="http://localhost", follow_redirects=False)  # no cookie from the login above
    r = fresh.post("/auth", data={"token": "nope", "next": "/"})
    assert r.status_code == 401


def test_page_token_visit_redirects_clean():
    app = create_app(Config(), secret="sekrit")
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    r = client.get("/", params={"token": "sekrit", "view": "table"})
    assert r.status_code == 303
    assert r.headers["location"] == "/?view=table"
    assert "herdrhq_auth" in r.headers.get("set-cookie", "")
