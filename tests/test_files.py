import shutil
import subprocess

import pytest
from starlette.testclient import TestClient

from herdrhq.app import create_app
from herdrhq.config import Config, HostSpec

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def local_app():
    cfg = Config(auth_enabled=False)
    cfg.hosts["local"] = HostSpec(name="local", transport="local")
    return create_app(cfg)


def test_ls_local(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / ".hidden").write_text("x")
    client = TestClient(local_app(), base_url="http://localhost")
    r = client.get("/api/fs/ls", params={"host": "local", "path": str(tmp_path)})
    assert r.status_code == 200
    data = r.json()
    assert data["host"] == "local"
    assert data["path"] == str(tmp_path)
    names = [e["name"] for e in data["entries"]]
    assert names == ["sub", ".hidden", "a.txt"]  # dirs first, then case-insensitive
    byname = {e["name"]: e for e in data["entries"]}
    assert byname["sub"]["dir"] is True
    assert byname["a.txt"]["size"] == 5


def test_ls_missing_params():
    client = TestClient(local_app(), base_url="http://localhost")
    assert client.get("/api/fs/ls").status_code == 400
    r = client.get("/api/fs/ls", params={"host": "local", "path": "/definitely/not/here"})
    assert r.status_code == 502


def test_file_stream_and_download(tmp_path):
    p = tmp_path / "report.log"
    p.write_text("line1\nline2\n")
    client = TestClient(local_app(), base_url="http://localhost")
    r = client.get("/api/fs/file", params={"host": "local", "path": str(p)})
    assert r.status_code == 200
    assert r.text == "line1\nline2\n"
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-length"] == "12"
    r = client.get("/api/fs/file", params={"host": "local", "path": str(p), "dl": "1"})
    assert "attachment" in r.headers["content-disposition"]


def test_markdown_served_raw(tmp_path):
    p = tmp_path / "README.md"
    p.write_text("# hi")
    client = TestClient(local_app(), base_url="http://localhost")
    r = client.get("/api/fs/file", params={"host": "local", "path": str(p)})
    assert r.headers["content-type"].startswith("text/markdown")


def test_watch_emits_init_then_detach(tmp_path):
    # An infinite SSE stream can't be driven through buffering HTTP test
    # transports — exercise the generator directly.
    import asyncio
    from urllib.parse import urlencode

    from starlette.requests import Request

    from herdrhq.files import FileRoutes
    from herdrhq.pool import SSHPool

    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-fake")

    async def go():
        cfg = Config(auth_enabled=False)
        cfg.hosts["local"] = HostSpec(name="local", transport="local")
        fr = FileRoutes(cfg, SSHPool())
        scope = {
            "type": "http", "method": "GET", "headers": [],
            "query_string": urlencode(
                {"host": "local", "path": str(p), "interval": "0.5"}).encode(),
        }
        resp = await fr.watch(Request(scope))
        gen = resp.body_iterator
        first = await asyncio.wait_for(gen.__anext__(), 5)
        assert '"type": "init"' in first
        [w] = fr.watchers.values()
        assert w["path"] == str(p)
        w["close"].set()
        async for chunk in gen:
            if "bye" in chunk:
                break
        else:
            raise AssertionError("stream ended without a bye")
        await gen.aclose()  # run the generator's finally, as a disconnect would
        assert fr.watchers == {}

    asyncio.run(go())


def test_hosts_endpoint():
    client = TestClient(local_app(), base_url="http://localhost")
    r = client.get("/api/hosts")
    assert r.status_code == 200
    [h] = r.json()
    assert h["name"] == "local"
    assert h["kind"] == "local"
    assert h["connected"] is True


def test_pages_served():
    client = TestClient(local_app(), base_url="http://localhost")
    assert "Workspace" in client.get("/work").text
    assert "viewer" in client.get("/view").text
    assert client.get("/favicon.svg").status_code == 200
    r = client.get("/browse", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/work"


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True)


@needs_git
def test_git_show_head(tmp_path):
    _git(tmp_path, "init", "-q")
    p = tmp_path / "notes.md"
    p.write_text("v1\n")
    _git(tmp_path, "add", "notes.md")
    _git(tmp_path, "commit", "-q", "-m", "v1")
    p.write_text("v2\n")  # working copy moves on; HEAD stays v1
    client = TestClient(local_app(), base_url="http://localhost")
    r = client.get("/api/fs/git-show", params={"host": "local", "path": str(p)})
    assert r.status_code == 200
    data = r.json()
    assert data["content"] == "v1\n"
    assert data["relpath"] == "notes.md"
    assert data["ref"] == "HEAD"
    # macOS resolves /var -> /private/var; compare resolved paths
    assert data["toplevel"] == str(tmp_path.resolve())


@needs_git
def test_git_show_not_a_repo(tmp_path):
    p = tmp_path / "loose.txt"
    p.write_text("no repo here")
    client = TestClient(local_app(), base_url="http://localhost")
    r = client.get("/api/fs/git-show", params={"host": "local", "path": str(p)})
    assert r.status_code == 404
    assert r.json()["reason"] == "not_a_repo"


@needs_git
def test_git_show_untracked(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("committed")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "init")
    p = tmp_path / "new.txt"
    p.write_text("not committed yet")
    client = TestClient(local_app(), base_url="http://localhost")
    r = client.get("/api/fs/git-show", params={"host": "local", "path": str(p)})
    assert r.status_code == 404
    assert r.json()["reason"] == "not_in_ref"


def test_git_show_bad_params(tmp_path):
    client = TestClient(local_app(), base_url="http://localhost")
    assert client.get("/api/fs/git-show").status_code == 400
    r = client.get("/api/fs/git-show",
                   params={"host": "local", "path": str(tmp_path), "ref": "--help"})
    assert r.status_code == 400
    r = client.get("/api/fs/git-show",
                   params={"host": "local", "path": str(tmp_path), "ref": "bad ref;rm"})
    assert r.status_code == 400


@needs_git
def test_git_show_binary(tmp_path):
    _git(tmp_path, "init", "-q")
    p = tmp_path / "blob.bin"
    p.write_bytes(b"a\x00b")
    _git(tmp_path, "add", "blob.bin")
    _git(tmp_path, "commit", "-q", "-m", "bin")
    client = TestClient(local_app(), base_url="http://localhost")
    r = client.get("/api/fs/git-show", params={"host": "local", "path": str(p)})
    assert r.status_code == 415
    assert r.json()["reason"] == "binary"
