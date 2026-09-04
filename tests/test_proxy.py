from starlette.testclient import TestClient

from herdrhq.app import create_app
from herdrhq.config import Config, HostSpec, HttpService
from herdrhq.proxy import match_service, service_fid


def make_cfg():
    cfg = Config(listen_port=9000)
    cfg.hosts["local"] = HostSpec(name="local", transport="local")
    cfg.hosts["mercury"] = HostSpec(
        name="mercury",
        http=[HttpService(name="jupyter", remote_host="localhost", remote_port=8888)],
    )
    return cfg


def test_service_fid():
    assert service_fid("mercury", "jupyter") == "http:mercury:jupyter"


def test_match_service_declared_and_dynamic():
    cfg = make_cfg()
    hs, s = match_service(cfg, "jupyter.mercury.localhost")
    assert hs.name == "mercury" and s.remote_port == 8888
    hs, s = match_service(cfg, "p8877.mercury.localhost")
    assert s.name == "p8877" and s.remote_port == 8877 and s.remote_host == "localhost"
    assert match_service(cfg, "p8877.nowhere.localhost") is None
    assert match_service(cfg, "jupyter.mercury.example.com") is None
    assert match_service(cfg, "p99999.mercury.localhost") is None  # not a port
    assert match_service(cfg, "mercury.localhost") is None  # no service label


def test_host_guard():
    app = create_app(make_cfg(), secret="hunter2")
    c = TestClient(app, base_url="http://localhost")
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 421


def test_service_origin_auth_bounce():
    app = create_app(make_cfg(), secret="hunter2")
    c = TestClient(app, base_url="http://jupyter.mercury.localhost:9000")
    # Browser visit without this origin's cookie: bounced to /__bless on the
    # main origin (login page there, or straight back if already signed in).
    r = c.get("/nb?x=1", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == (
        "http://localhost:9000/__bless?"
        "to=http%3A%2F%2Fjupyter.mercury.localhost%3A9000%2Fnb%3Fx%3D1"
    )
    # Non-browser clients still get a plain 401.
    assert c.get("/nb").status_code == 401
    # A correct token blesses the origin: cookie + redirect to the clean URL.
    r = c.get("/nb", params={"hq_token": "hunter2"}, follow_redirects=False)
    assert r.status_code == 303 and "herdrhq_auth" in r.headers.get("set-cookie", "")
    assert r.headers["location"] == "/nb"


def test_bless_endpoint():
    app = create_app(make_cfg(), secret="hunter2")
    c = TestClient(app, base_url="http://localhost")
    c.headers["Authorization"] = "Bearer hunter2"
    good = "http://jupyter.mercury.localhost:9000/lab"
    r = c.get("/__bless", params={"to": good}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"{good}?hq_token=hunter2"
    # Dynamic p<port> origins are ours too.
    dyn = "http://p8877.mercury.localhost:9000/"
    r = c.get("/__bless", params={"to": dyn}, follow_redirects=False)
    assert r.status_code == 303
    # Anything but our own service URLs is refused: the token must not leak.
    for bad in (
        "http://evil.example/",
        "http://other.mercury.localhost:9000/",
        "https://jupyter.mercury.localhost:9000/",
        "http://jupyter.mercury.localhost:1234/",
        "",
    ):
        assert c.get("/__bless", params={"to": bad}, follow_redirects=False).status_code == 400


def test_services_listing():
    app = create_app(make_cfg(), secret="hunter2")
    c = TestClient(app, base_url="http://localhost")
    c.headers["Authorization"] = "Bearer hunter2"
    [svc] = c.get("/api/services").json()
    assert svc["name"] == "jupyter" and svc["host"] == "mercury"
    assert svc["url"] == "http://jupyter.mercury.localhost:9000/?hq_token=hunter2"
    assert svc["up"] is False  # never connected in tests


def test_preview_local_host_passthrough():
    app = create_app(make_cfg(), secret="hunter2")
    c = TestClient(app, base_url="http://localhost")
    c.headers["Authorization"] = "Bearer hunter2"
    r = c.post("/api/preview", json={"host": "local", "port": 8765})
    assert r.status_code == 200
    data = r.json()
    assert data["service"] == "p8765"
    assert data["local_port"] == 8765  # no ssh forward: the port is already here
    assert data["url"].startswith("http://p8765.local.localhost:9000/")


def test_preview_validation():
    app = create_app(make_cfg(), secret="hunter2")
    c = TestClient(app, base_url="http://localhost")
    c.headers["Authorization"] = "Bearer hunter2"
    assert c.post("/api/preview", json={"host": "nowhere", "port": 80}).status_code == 400
    assert c.post("/api/preview", json={"host": "local", "port": 0}).status_code == 400
    assert c.post("/api/preview", json={"host": "local"}).status_code == 400


def test_proxy_relays_to_local_port():
    """End to end through ProxyRouter._http against a real local HTTP server."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = f"hello from {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        app = create_app(make_cfg(), secret="hunter2")
        c = TestClient(app, base_url=f"http://p{port}.local.localhost:9000")
        c.cookies.set("herdrhq_auth", "hunter2")
        r = c.get("/some/path?a=1")
        assert r.status_code == 200
        assert r.text == "hello from /some/path?a=1"
    finally:
        srv.shutdown()
