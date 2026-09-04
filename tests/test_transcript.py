import json

from herdrhq.transcript import munge_cwd, parse_claude_tail, resolve_path


def jl(*records) -> bytes:
    # a leading partial line, as a real tail seek would produce
    return b'{"cut mid-rec' + b"\n" + b"\n".join(json.dumps(r).encode() for r in records) + b"\n"


def test_munge_cwd():
    assert munge_cwd("/Users/tlamadon/git/herdr-hq") == "-Users-tlamadon-git-herdr-hq"
    assert munge_cwd("/home/u/pro.ject_x") == "-home-u-pro-ject-x"


def test_resolve_path_kinds():
    path, why = resolve_path({"agent_session": {"kind": "path", "value": "/x/t.jsonl"}})
    assert path == "/x/t.jsonl" and why is None
    path, why = resolve_path({
        "agent_session": {"kind": "id", "value": "abc-123", "agent": "claude"},
        "cwd": "/home/u/proj",
    })
    assert path == ".claude/projects/-home-u-proj/abc-123.jsonl"
    path, why = resolve_path({"agent_session": None})
    assert path is None and "not reported" in why
    path, why = resolve_path({
        "agent_session": {"kind": "id", "value": "x", "agent": "codex"}, "cwd": "/p",
    })
    assert path is None and "unsupported agent" in why


def test_parse_prompt_tool_and_answer():
    blob = jl(
        {"type": "user", "message": {"content": "fix the flaky test"},
         "timestamp": "2026-09-04T10:00:00Z"},
        {"type": "assistant", "message": {"model": "claude-fable-5", "content": [
            {"type": "text", "text": "Let me re-run the suite."},
            {"type": "tool_use", "id": "tu_1", "name": "Bash",
             "input": {"command": "pytest -q tests/"}},
        ]}, "timestamp": "2026-09-04T10:00:05Z"},
    )
    s = parse_claude_tail(blob)
    assert s.last_prompt == "fix the flaky test"
    assert s.last_assistant == "Let me re-run the suite."
    assert s.current_tool == {"name": "Bash", "detail": "pytest -q tests/"}
    assert s.model == "claude-fable-5"
    assert s.ts == "2026-09-04T10:00:05Z"


def test_resolved_tool_is_not_current():
    blob = jl(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "ls"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "All done — 42 tests pass."},
        ]}},
    )
    s = parse_claude_tail(blob)
    assert s.current_tool is None
    assert s.last_assistant == "All done — 42 tests pass."


def test_last_prompt_record_wins_and_sidechains_skip():
    blob = jl(
        {"type": "user", "message": {"content": "subagent chatter"}, "isSidechain": True},
        {"type": "last-prompt", "lastPrompt": "the real ask"},
        {"type": "ai-title", "aiTitle": "Fixing the build"},
        {"type": "assistant", "isSidechain": True,
         "message": {"content": [{"type": "text", "text": "sidechain answer"}]}},
    )
    s = parse_claude_tail(blob)
    assert s.last_prompt == "the real ask"
    assert s.title == "Fixing the build"
    assert s.last_assistant is None  # only a sidechain answered


def test_tool_result_only_user_is_not_a_prompt():
    blob = jl(
        {"type": "user", "message": {"content": "actual question"}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_9", "content": "output"},
        ]}},
    )
    s = parse_claude_tail(blob)
    assert s.last_prompt == "actual question"


def test_garbage_and_local_commands_skipped():
    blob = jl(
        {"type": "user", "message": {"content": "<local-command-stdout>x</local-command-stdout>"}},
        {"type": "queue-operation", "op": "x"},
    ) + b"not json at all\n"
    s = parse_claude_tail(blob)
    assert s.last_prompt is None


def test_real_transcript_parses():
    """The dogfood check: this very session's transcript, if present."""
    from pathlib import Path

    d = Path.home() / ".claude" / "projects"
    files = sorted(d.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime) if d.exists() else []
    if not files:
        return  # machine without Claude Code; the fixtures above cover the logic
    blob = files[-1].read_bytes()[-262144:]
    s = parse_claude_tail(blob)
    assert s.last_prompt or s.last_assistant or s.current_tool


# ---------------------------------------------------------------- messages


def test_parse_messages_conversation():
    from herdrhq.transcript import parse_claude_messages

    blob = jl(
        {"type": "user", "message": {"content": "fix the bug"},
         "timestamp": "2026-09-04T10:00:00Z"},
        {"type": "assistant", "message": {"model": "claude-fable-5", "content": [
            {"type": "text", "text": "Looking at it now."},
        ]}, "timestamp": "2026-09-04T10:00:03Z"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "/repo/a.py"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "…"},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Found it: an off-by-one in `range`."},
        ]}, "timestamp": "2026-09-04T10:00:09Z"},
        {"type": "user", "message": {"content": "great, fix it"}},
    )
    msgs = parse_claude_messages(blob)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["text"] == "fix the bug"
    a = msgs[1]  # one merged turn: text + tool + more text
    assert a["text"] == "Looking at it now.\n\nFound it: an off-by-one in `range`."
    assert a["tools"] == [{"name": "Read", "detail": "/repo/a.py"}]
    assert a["model"] == "claude-fable-5"
    assert msgs[2]["text"] == "great, fix it"


def test_parse_messages_skips_noise_and_limits():
    from herdrhq.transcript import parse_claude_messages

    recs = [{"type": "user", "message": {"content": f"q{i}"}} for i in range(10)]
    noise = [
        {"type": "user", "isSidechain": True, "message": {"content": "sub"}},
        {"type": "user", "isMeta": True, "message": {"content": "meta"}},
        {"type": "user", "message": {"content": "<local-command-stdout>x</local-command-stdout>"}},
        {"type": "system", "content": "sys"},
        {"type": "ai-title", "aiTitle": "t"},
    ]
    msgs = parse_claude_messages(jl(*recs, *noise), limit=4)
    assert [m["text"] for m in msgs] == ["q6", "q7", "q8", "q9"]


def test_mentioned_files_order_and_dedupe():
    from herdrhq.transcript import mentioned_files

    def tool(path, key="file_path"):
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "x", "name": "Edit", "input": {key: path}},
        ]}}

    blob = jl(
        tool("/repo/a.py"),
        tool("/repo/b.py", key="notebook_path"),
        tool("relative/nope.py"),      # not absolute: ignored
        tool("/repo/a.py"),            # re-mention promotes to newest
    )
    assert mentioned_files(blob) == ["/repo/a.py", "/repo/b.py"]
    assert mentioned_files(blob, cap=1) == ["/repo/a.py"]


def test_messages_endpoint_degrades(tmp_path):
    from starlette.testclient import TestClient

    from herdrhq.app import create_app
    from herdrhq.config import Config, HostSpec

    cfg = Config(auth_enabled=False)
    cfg.hosts["local"] = HostSpec(name="local", transport="local")
    app = create_app(cfg)
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/api/transcript/messages",
                      params={"host": "nope", "pane": "x"}).status_code == 404


def test_locate_guesses_newest_transcript(tmp_path, monkeypatch):
    """No agent_session: fall back to the newest .jsonl for the pane's cwd."""
    import asyncio
    import os

    from herdrhq.files import LocalFs
    from herdrhq.transcript import TranscriptPeek

    monkeypatch.setenv("HOME", str(tmp_path))  # LocalFs resolves relative to home
    d = tmp_path / ".claude" / "projects" / "-work-proj"
    d.mkdir(parents=True)
    (d / "old.jsonl").write_text('{"type":"user","message":{"content":"old"}}\n')
    (d / "new.jsonl").write_text('{"type":"user","message":{"content":"new"}}\n')
    os.utime(d / "old.jsonl", (1000, 1000))
    os.utime(d / "new.jsonl", (2000, 2000))

    peek = TranscriptPeek(lambda host: LocalFs())
    pane = {"is_agent": True, "agent": "claude", "cwd": "/work/proj", "agent_session": None}

    async def go():
        path, reason, guessed = await peek.locate("local", pane)
        assert guessed and path.endswith("new.jsonl") and reason is None
        out = await peek.messages("local", pane)
        assert out["available"] and out["guessed"]
        assert out["messages"][-1]["text"] == "new"
        # unknown cwd degrades with the reason chained
        missing = {"is_agent": True, "agent": "claude", "cwd": "/nowhere", "agent_session": None}
        path2, reason2, _ = await peek.locate("local", missing)
        assert path2 is None and "no transcripts" in reason2
        return True

    assert asyncio.run(go())
