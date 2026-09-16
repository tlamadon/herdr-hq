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
            {"type": "thinking", "thinking": "off-by-one, probably"},
            {"type": "text", "text": "Looking at it now."},
        ]}, "timestamp": "2026-09-04T10:00:03Z"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "/repo/a.py"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "1: x=1"},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Found it: an off-by-one in `range`."},
        ]}, "timestamp": "2026-09-04T10:00:09Z"},
        {"type": "user", "message": {"content": "great, fix it"}},
    )
    msgs = parse_claude_messages(blob)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["blocks"] == [{"t": "text", "text": "fix the bug"}]
    a = msgs[1]  # one merged turn, blocks in transcript order
    assert [b["t"] for b in a["blocks"]] == ["thinking", "text", "tool", "text"]
    assert a["blocks"][1]["text"] == "Looking at it now."
    tool = a["blocks"][2]
    assert tool["name"] == "Read" and tool["input"] == {"file_path": "/repo/a.py"}
    assert tool["result"] == "1: x=1" and "pending" not in tool and "error" not in tool
    assert a["blocks"][3]["text"] == "Found it: an off-by-one in `range`."
    assert a["model"] == "claude-fable-5"
    assert msgs[2]["blocks"][0]["text"] == "great, fix it"


def test_parse_messages_error_and_pending_tools():
    from herdrhq.transcript import parse_claude_messages

    blob = jl(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "false"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "Exit code 1",
             "is_error": True},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "sleep 99"}},
        ]}},
    )
    (a,) = parse_claude_messages(blob)
    failed, running = a["blocks"]
    assert failed["error"] and failed["result"] == "Exit code 1"
    assert running["pending"] and "result" not in running


def test_parse_messages_skips_noise_and_limits():
    from herdrhq.transcript import parse_claude_messages

    recs = [{"type": "user", "message": {"content": f"q{i}"}} for i in range(10)]
    noise = [
        {"type": "user", "isSidechain": True, "message": {"content": "sub"}},
        {"type": "user", "isMeta": True, "message": {"content": "meta"}},
        {"type": "user", "message": {"content": "<system-reminder>ctx</system-reminder>"}},
        {"type": "user", "message": {"content": "Caveat: injected preamble"}},
        {"type": "user", "message": {"content": "[Request interrupted by user]"}},
        {"type": "system", "content": "sys"},
        {"type": "ai-title", "aiTitle": "t"},
    ]
    msgs = parse_claude_messages(jl(*recs, *noise), limit=4)
    assert [m["blocks"][0]["text"] for m in msgs] == ["q6", "q7", "q8", "q9"]


def test_command_rows_fold_their_stdout():
    from herdrhq.transcript import parse_claude_messages

    blob = jl(
        {"type": "user", "message": {"content":
            "<command-name>/context</command-name>\n<command-args>full</command-args>"}},
        {"type": "user", "message": {"content":
            "<local-command-stdout>ctx: 42k</local-command-stdout>"}},
    )
    (msg,) = parse_claude_messages(blob)
    assert msg["blocks"] == [
        {"t": "command", "name": "/context", "args": "full", "output": "ctx: 42k"},
    ]


def test_superseded_edit_branch_is_pruned():
    from herdrhq.transcript import parse_claude_messages

    blob = jl(
        {"type": "user", "uuid": "u1", "parentUuid": "root",
         "message": {"content": "first ask"}},
        # abandoned attempt and its reply…
        {"type": "user", "uuid": "u2", "parentUuid": "u1",
         "message": {"content": "do it wrong"}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "u2",
         "message": {"content": [{"type": "text", "text": "wrong way it is"}]}},
        # …replaced by an edited prompt sharing the same parent
        {"type": "user", "uuid": "u3", "parentUuid": "u1",
         "message": {"content": "do it right"}},
        {"type": "assistant", "uuid": "a3", "parentUuid": "u3",
         "message": {"content": [{"type": "text", "text": "right away"}]}},
    )
    msgs = parse_claude_messages(blob)
    texts = [m["blocks"][0]["text"] for m in msgs]
    assert texts == ["first ask", "do it right", "right away"]


def test_compact_summary_is_a_system_row():
    from herdrhq.transcript import parse_claude_messages

    blob = jl(
        {"type": "user", "isCompactSummary": True, "isMeta": True,
         "message": {"content": "Earlier: we fixed the parser."}},
        {"type": "user", "message": {"content": "continue"}},
    )
    msgs = parse_claude_messages(blob)
    assert msgs[0]["role"] == "system"
    assert msgs[0]["blocks"][0] == {"t": "compact", "text": "Earlier: we fixed the parser."}


def test_claude_session_meta():
    from herdrhq.transcript import claude_session_meta

    blob = jl(
        {"type": "ai-title", "aiTitle": "Fixing the build"},
        {"type": "assistant", "message": {"model": "claude-fable-5", "usage": {
            "input_tokens": 12, "cache_read_input_tokens": 40000,
            "cache_creation_input_tokens": 2000, "output_tokens": 90}, "content": []}},
        # an interrupted turn writes an all-zero usage row — must not win
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "output_tokens": 0}, "content": []}},
    )
    meta = claude_session_meta(blob)
    assert meta["title"] == "Fixing the build"
    assert meta["model"] == "claude-fable-5"
    assert meta["context"] == 42012


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
        path, reason, guessed, fmt = await peek.locate("local", pane)
        assert guessed and path.endswith("new.jsonl") and reason is None and fmt == "claude"
        out = await peek.messages("local", pane)
        assert out["available"] and out["guessed"]
        assert out["messages"][-1]["blocks"][0]["text"] == "new"
        # unknown cwd degrades with the reason chained
        missing = {"is_agent": True, "agent": "claude", "cwd": "/nowhere", "agent_session": None}
        path2, reason2, _, _ = await peek.locate("local", missing)
        assert path2 is None and "no Claude transcripts" in reason2
        return True

    assert asyncio.run(go())


# ------------------------------------------------------------------- codex


def codex_blob():
    return jl(
        {"timestamp": "2026-09-04T10:00:00Z", "type": "session_meta",
         "payload": {"id": "abc", "cwd": "/work/proj", "model_provider": "openai"}},
        {"timestamp": "2026-09-04T10:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "add host uoc-ultra"}},
        {"timestamp": "2026-09-04T10:00:02Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "Inspecting the layout first."}},
        {"timestamp": "2026-09-04T10:00:03Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1",
                     "arguments": "{\"cmd\": \"find hosts -type f\"}"}},
        {"timestamp": "2026-09-04T10:00:04Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c1", "output": "ok"}},
        {"timestamp": "2026-09-04T10:00:05Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "Added the host."}},
        {"timestamp": "2026-09-04T10:00:06Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "call_id": "c2",
                     "arguments": "{\"cmd\": \"nix build\"}"}},
    )


def test_parse_codex_messages():
    from herdrhq.transcript import parse_codex_messages

    msgs = parse_codex_messages(codex_blob())
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["blocks"] == [{"t": "text", "text": "add host uoc-ultra"}]
    a = msgs[1]
    assert [b["t"] for b in a["blocks"]] == ["text", "tool", "text", "tool"]
    first, second = a["blocks"][1], a["blocks"][3]
    assert first["input"] == {"cmd": "find hosts -type f"}
    assert first["result"] == "ok" and "pending" not in first
    assert second["input"] == {"cmd": "nix build"} and second["pending"]


def test_parse_codex_items_format():
    """Modern rollouts (cli >= ~0.150) journal conversation as item_completed
    items; raw tool calls and developer messages must not duplicate them."""
    from herdrhq.transcript import parse_codex_messages

    def item(payload):
        return {"timestamp": "2026-09-15T22:09:00Z", "type": "event_msg",
                "payload": {"type": "item_completed", "item": payload}}

    blob = jl(
        # injected context that must stay hidden
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
         "content": [{"type": "input_text", "text": "<skills_instructions>…"}]}},
        item({"type": "UserMessage", "content": [{"type": "text", "text": "prove the bound"}]}),
        item({"type": "Reasoning", "summary_text": [], "raw_content": []}),
        item({"type": "AgentMessage", "content": [{"type": "Text", "text": "Reading the notes first."}]}),
        # the raw call for the same run — must be ignored in favour of the item
        {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "c1",
         "name": "exec", "input": 'text(await tools.exec_command({cmd:"rg bounds"}));'}},
        item({"type": "CommandExecution",
              "command": ["/bin/zsh", "-lc", "rg bounds"],
              "parsed_cmd": [{"type": "unknown", "cmd": "rg bounds"}],
              "status": "completed", "exit_code": 0,
              "stdout": "notes.tex:12", "aggregated_output": "notes.tex:12"}),
        item({"type": "CommandExecution", "command": ["/bin/zsh", "-lc", "false"],
              "status": "failed", "exit_code": 1, "aggregated_output": "boom"}),
        item({"type": "FileChange", "changes": {
            "/repo/new.tex": {"type": "add", "content": "\\documentclass{article}"},
            "/repo/old.tex": {"type": "update", "unified_diff": "-a\n+b", "move_path": None},
        }}),
        item({"type": "AgentMessage", "content": [{"type": "Text", "text": "Done."}]}),
    )
    msgs = parse_codex_messages(blob)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["blocks"] == [{"t": "text", "text": "prove the bound"}]
    blocks = msgs[1]["blocks"]
    assert [b["t"] for b in blocks] == ["text", "tool", "tool", "tool", "tool", "text"]
    ok, failed, write, edit = blocks[1], blocks[2], blocks[3], blocks[4]
    assert ok["name"] == "Bash" and ok["input"]["command"] == "rg bounds"
    assert ok["result"] == "notes.tex:12" and "error" not in ok
    assert failed["error"] and failed["result"] == "boom"
    assert write["name"] == "Write" and write["input"]["file_path"] == "/repo/new.tex"
    assert edit["name"] == "Edit" and edit["input"]["unified_diff"] == "-a\n+b"


def test_codex_meta_model_from_turn_context():
    from herdrhq.transcript import codex_session_meta

    blob = jl(
        {"type": "turn_context", "payload": {"model": "gpt-6-astra", "effort": "high"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"total_tokens": 85039},
            "model_context_window": 258400}}},
    )
    meta = codex_session_meta(blob)
    assert meta == {"model": "gpt-6-astra", "context": 85039, "window": 258400}


def test_codex_output_envelope_and_meta():
    from herdrhq.transcript import _codex_output, codex_session_meta

    text, err = _codex_output('{"output": "boom", "metadata": {"exit_code": 2}}')
    assert text == "boom" and err
    text, err = _codex_output("plain text")
    assert text == "plain text" and not err

    blob = jl(
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 900, "output_tokens": 100,
                                  "total_tokens": 1000},
            "model_context_window": 272000}}},
    )
    assert codex_session_meta(blob) == {"context": 1000, "window": 272000}


def test_parse_codex_tail_current_tool():
    from herdrhq.transcript import parse_codex_tail

    s = parse_codex_tail(codex_blob())
    assert s.last_prompt == "add host uoc-ultra"
    assert s.last_assistant == "Added the host."
    # c1 got its output; c2 has none and nothing follows it — still running
    assert s.current_tool == {"name": "exec_command", "detail": "nix build"}


def test_codex_find_by_cwd_and_id(tmp_path, monkeypatch):
    import asyncio
    import json as jsonlib
    import os

    from herdrhq.files import LocalFs
    from herdrhq.transcript import TranscriptPeek

    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".codex" / "sessions" / "2026" / "09" / "04"
    d.mkdir(parents=True)

    def rollout(name, cwd, mtime, **meta):
        p = d / name
        p.write_text(jsonlib.dumps(
            {"type": "session_meta", "payload": {"id": name, "cwd": cwd, **meta}}) + "\n")
        os.utime(p, (mtime, mtime))

    t0 = 1_789_400_000
    rollout("rollout-2026-09-04T10-00-00-aaa.jsonl", "/other/place", t0)
    rollout("rollout-2026-09-04T11-00-00-bbb.jsonl", "/work/proj", t0 + 10)
    # older name but freshest main-thread write: activity outranks the filename
    rollout("rollout-2026-09-04T09-00-00-ccc.jsonl", "/work/proj", t0 + 100)
    # a sub-thread rollout never matches a cwd guess, however fresh
    rollout("rollout-2026-09-04T12-00-00-zzz.jsonl", "/work/proj", t0 + 200,
            parent_thread_id="rollout-2026-09-04T09-00-00-ccc.jsonl")

    peek = TranscriptPeek(lambda host: LocalFs())
    pane = {"is_agent": True, "agent": "codex", "cwd": "/work/proj", "agent_session": None}

    async def go():
        path, reason, guessed, fmt = await peek.locate("local", pane)
        assert path and path.endswith("ccc.jsonl") and guessed and fmt == "codex"
        by_id = {"is_agent": True, "agent": "codex", "cwd": "/x",
                 "agent_session": {"kind": "id", "agent": "codex", "value": "aaa"}}
        path2, _, guessed2, fmt2 = await peek.locate("local", by_id)
        assert path2 and path2.endswith("aaa.jsonl") and not guessed2 and fmt2 == "codex"
        nowhere = {"is_agent": True, "agent": "codex", "cwd": "/nope", "agent_session": None}
        path3, reason3, _, _ = await peek.locate("local", nowhere)
        assert path3 is None and "no codex session" in reason3
        return True

    assert asyncio.run(go())


def test_unsupported_agent_reason():
    import asyncio

    from herdrhq.files import LocalFs
    from herdrhq.transcript import TranscriptPeek

    peek = TranscriptPeek(lambda host: LocalFs())
    pane = {"is_agent": True, "agent": "gemini", "cwd": "/p", "agent_session": None}

    async def go():
        path, reason, _, _ = await peek.locate("local", pane)
        assert path is None and "not supported for gemini" in reason
        return True

    assert asyncio.run(go())


def test_codex_head_cwd_truncated():
    from herdrhq.transcript import _codex_head_cwd

    # a header cut mid-instructions, as a bounded read produces
    head = (b'{"timestamp":"t","type":"session_meta","payload":{"id":"x",'
            b'"cwd":"/home/u/work tree","originator":"codex_cli_rs",'
            b'"base_instructions":{"text":"You are Codex, based on GPT')
    assert _codex_head_cwd(head) == "/home/u/work tree"
    assert _codex_head_cwd(b"garbage") is None


def test_codex_mentioned_files():
    from herdrhq.transcript import codex_mentioned_files

    blob = jl(
        {"type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c1",
                     "input": 'text(await tools.exec_command({cmd:"cat notes.md README.md"}));'}},
        {"type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c2",
                     "input": 'tools.apply_patch("*** Begin Patch\\n*** Update File: /work/proj/plan.md\\n@@")'}},
        {"type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "call_id": "c3",
                     "arguments": '{"cmd": "rg -g \'!uv.lock\' TODO; open http://x.com/a.md; '
                                  'cat > docs/day-2.md <<EOF\\nsee notes.md\\nEOF"}'}},
    )
    files = codex_mentioned_files(blob, "/work/proj")
    # newest first; notes.md re-mentioned in c3 so it outranks README.md;
    # the negated glob and the URL never make it in. Relative tokens fan out
    # over cwd and the redirect target's dir — the wrong guesses (docs/docs/…)
    # are junk the caller's stat filter drops.
    assert files == [
        "/work/proj/docs/notes.md",
        "/work/proj/notes.md",
        "/work/proj/docs/docs/day-2.md",
        "/work/proj/docs/day-2.md",
        "/work/proj/plan.md",
        "/work/proj/README.md",
    ]
    # without a cwd only absolute paths survive
    assert codex_mentioned_files(blob, None) == ["/work/proj/plan.md"]


def test_codex_mentioned_files_link_bases_and_file_urls():
    from herdrhq.transcript import codex_mentioned_files

    blob = jl(
        {"type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c1",
                     "input": '*** Begin Patch\n*** Update File: slides/intro.md\n'
                              '+[next](session-1.html) and [lab](../labs/lab1.html)\n'
                              '*** End Patch'}},
        {"type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "call_id": "c2",
                     "arguments": '{"cmd": "open file:///work/proj/slides/out.html"}'}},
        {"type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "call_id": "c3",
                     "arguments": '{"cmd": "cat > labs/README.md <<EOF\\n'
                                  '[deck](../slides/session-2.html)\\nEOF"}'}},
    )
    files = codex_mentioned_files(blob, "/work/proj")
    # markdown links resolve against the patched/redirected file's dir as
    # well as cwd; the file:// URL survives the URL colon guard
    for want in ("/work/proj/slides/intro.md",
                 "/work/proj/slides/session-1.html",
                 "/work/proj/labs/lab1.html",
                 "/work/proj/slides/out.html",
                 "/work/proj/labs/README.md",
                 "/work/proj/slides/session-2.html"):
        assert want in files
    # the patched file's dir gives content links a home even without a cwd
    assert "slides/lab1.html" not in files
    files_nocwd = codex_mentioned_files(jl(
        {"type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c3",
                     "input": '*** Update File: /work/proj/docs/guide.md\n'
                              '+see [logo](../assets/logo.png)'}}), None)
    assert "/work/proj/assets/logo.png" in files_nocwd


def test_existing_files_filters_and_caps(tmp_path):
    import asyncio

    from herdrhq.files import LocalFs
    from herdrhq.transcript import existing_files

    real = tmp_path / "a.md"
    real.write_text("x")
    kept = asyncio.run(existing_files(
        LocalFs(), [str(real), str(tmp_path / "gone.md"), str(real)]))
    assert kept == [str(real), str(real)]
    assert asyncio.run(existing_files(LocalFs(), [str(real)], cap=0)) == []
