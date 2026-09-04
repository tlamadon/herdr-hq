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
    assert path == "~/.claude/projects/-home-u-proj/abc-123.jsonl"
    path, why = resolve_path({"agent_session": None})
    assert path is None and "integration" in why
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
