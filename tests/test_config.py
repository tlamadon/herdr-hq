import json

from herdrhq.config import Config, _parse, _parse_legacy, load_config


def test_defaults():
    cfg = _parse({}, None)
    assert cfg.listen_host == "127.0.0.1"
    assert cfg.listen_port == 8787
    assert cfg.auth_enabled is True
    assert cfg.password is None
    assert cfg.poll_interval == 6.0
    assert cfg.terminal_enabled is True
    assert cfg.hosts == {}


def test_parse_yaml_shape():
    data = {
        "listen": {"host": "0.0.0.0", "port": 9000, "password": "pw", "auth": "on"},
        "poll": {"interval": 3, "history": 30},
        "terminal": {"input": False},
        "events": {"enabled": False},
        "hosts": {
            "local": {"transport": "local"},
            "nixos": {},
            "cluster": {
                "target": "login01.example.edu",
                "python": "python3.12",
                "http": {"jupyter": 8888, "mlflow": "localhost:5000"},
                "tunnels": [5432, {"local": 18888, "remote": "db:9999"}],
            },
        },
    }
    cfg = _parse(data, None)
    assert cfg.listen_port == 9000
    assert cfg.password == "pw"
    assert cfg.poll_interval == 3.0
    assert cfg.history == 30
    assert cfg.terminal_input is False
    assert cfg.events_enabled is False

    assert cfg.hosts["local"].transport == "local"
    nixos = cfg.hosts["nixos"]
    assert nixos.transport == "ssh"
    assert nixos.ssh_target == "nixos"
    cluster = cfg.hosts["cluster"]
    assert cluster.ssh_target == "login01.example.edu"
    assert cluster.python == "python3.12"
    assert {s.name: (s.remote_host, s.remote_port) for s in cluster.http} == {
        "jupyter": ("localhost", 8888),
        "mlflow": ("localhost", 5000),
    }
    assert [(t.local, t.remote_host, t.remote_port) for t in cluster.tunnels] == [
        (5432, "localhost", 5432),
        (18888, "db", 9999),
    ]


def test_auth_none():
    cfg = _parse({"listen": {"auth": "none"}}, None)
    assert cfg.auth_enabled is False


def test_legacy_config_json():
    # the shape herdr-hq 0.1 shipped, including ssh options asyncssh ignores
    data = {
        "host": "127.0.0.1",
        "port": 8787,
        "poll_interval": 6.0,
        "sample_interval": 0.7,
        "ssh_timeout": 30.0,
        "history": 120,
        "hosts": [
            {"name": "local", "transport": "local"},
            {"name": "nixos", "transport": "ssh", "target": "nixos", "python": "python3"},
            {"name": "nixos-ultra", "transport": "ssh", "target": "nixos-ultra",
             "python": "python3", "ssh_args": ["-4"]},
        ],
    }
    cfg = _parse_legacy(data, None)
    assert cfg.poll_timeout == 30.0
    assert list(cfg.hosts) == ["local", "nixos", "nixos-ultra"]
    assert cfg.hosts["local"].transport == "local"
    assert cfg.hosts["nixos"].ssh_target == "nixos"
    assert cfg.hosts["nixos-ultra"].python == "python3"


def test_legacy_name_dedup():
    data = {"hosts": [{"target": "box"}, {"target": "box"}]}
    cfg = _parse_legacy(data, None)
    assert list(cfg.hosts) == ["box", "box'"]


def test_load_config_explicit_json(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"hosts": [{"name": "x", "target": "x"}]}))
    monkeypatch.chdir(tmp_path)
    cfg = load_config(str(p))
    assert list(cfg.hosts) == ["x"]


def test_load_config_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HERDRHQ_CONFIG", raising=False)
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert list(cfg.hosts) == ["localhost"]
    assert cfg.hosts["localhost"].transport == "local"
