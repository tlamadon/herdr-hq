"""Transcript peek: what an agent is actually doing, from its own session log.

herdr's AgentSessionInfo links a pane to the underlying agent's session —
either a transcript path outright, or a session id that (for Claude Code)
maps to ~/.claude/projects/<munged-cwd>/<session-id>.jsonl on the same
machine. Tailing that file over SFTP turns "working, 340% cpu" into
"working: running pytest — last said …".

The herdr integration must be installed on the host (`herdr integration
install claude`) for agent_session to be reported at all; everything here
degrades to explanatory absence rather than errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
from dataclasses import asdict, dataclass

log = logging.getLogger("herdrhq.transcript")

TAIL_BYTES = 256 * 1024
# browser-tool sessions embed screenshots as base64 in the JSONL; a small tail
# would hold two of those and no conversation. Re-read only on stat change.
CHAT_TAIL_BYTES = 1024 * 1024
SNIP = 400  # transcripts hold whole essays; cards need a line
CHAT_LIMIT = 80  # messages served to the chat view
FILES_CAP = 30


def munge_cwd(cwd: str) -> str:
    """Claude Code's project-directory name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def resolve_path(pane: dict) -> tuple[str | None, str | None]:
    """(remote transcript path, reason-if-none) for a pane's agent_session.

    Paths are home-relative (never ~-prefixed): SFTP resolves them against
    the remote home, and LocalFs mirrors that semantic.
    """
    sess = pane.get("agent_session")
    if not sess:
        return None, "agent session not reported"
    kind = sess.get("kind")
    value = sess.get("value")
    if not value:
        return None, "agent session has no value"
    if kind == "path":
        return str(value), None
    if kind == "id":
        agent = (sess.get("agent") or pane.get("agent") or "").lower()
        cwd = pane.get("cwd")
        if agent == "claude" and cwd:
            return f".claude/projects/{munge_cwd(cwd)}/{value}.jsonl", None
        return None, f"unsupported agent format: {agent or 'unknown'}"
    return None, f"unsupported agent session kind: {kind}"


def _snip(text: str, limit: int = SNIP) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_detail(inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "cmd", "description", "file_path", "path", "pattern",
                "prompt", "url", "query"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            return _snip(v, 120)
    return ""


@dataclass
class Summary:
    last_prompt: str | None = None
    last_assistant: str | None = None
    current_tool: dict | None = None  # {"name", "detail"}
    title: str | None = None
    model: str | None = None
    ts: str | None = None  # timestamp of the newest record seen


def parse_claude_tail(blob: bytes) -> Summary:
    """Summarize the tail of a Claude Code session JSONL.

    Pure and defensive: unknown record types are skipped, a truncated first
    line (from seeking into the file) parses as garbage and is dropped.
    """
    out = Summary()
    # a first line cut by the tail seek fails to parse and is skipped below
    lines = blob.split(b"\n")

    open_tool: dict | None = None  # newest assistant tool_use, awaiting a result
    resolved: set[str] = set()  # tool_use_ids that already got results

    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rtype = rec.get("type")
        if out.ts is None and rec.get("timestamp"):
            out.ts = rec["timestamp"]

        if rtype == "last-prompt" and out.last_prompt is None:
            if rec.get("lastPrompt"):
                out.last_prompt = _snip(str(rec["lastPrompt"]))
        elif rtype == "ai-title" and out.title is None:
            if rec.get("aiTitle"):
                out.title = _snip(str(rec["aiTitle"]), 120)
        elif rtype == "user" and not rec.get("isSidechain"):
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        resolved.add(block.get("tool_use_id") or "")
                    elif (out.last_prompt is None and isinstance(block, dict)
                          and block.get("type") == "text" and block.get("text")):
                        out.last_prompt = _snip(block["text"])
            elif (out.last_prompt is None and isinstance(content, str)
                  and content and not content.startswith("<")):
                out.last_prompt = _snip(content)
        elif rtype == "assistant" and not rec.get("isSidechain"):
            msg = rec.get("message") or {}
            if out.model is None and msg.get("model"):
                out.model = msg["model"]
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in reversed(content):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if (btype == "tool_use" and open_tool is None
                        and out.last_assistant is None
                        and block.get("id") not in resolved):
                    open_tool = {
                        "name": block.get("name") or "tool",
                        "detail": _tool_detail(block.get("input")),
                    }
                elif btype == "text" and out.last_assistant is None and block.get("text"):
                    out.last_assistant = _snip(block["text"])

        if out.last_prompt and out.last_assistant and out.title and out.model:
            break

    out.current_tool = open_tool
    return out


def _iter_records(blob: bytes):
    """Well-formed JSON records from a tail read, oldest first.

    A first line cut mid-record by the tail seek simply fails to parse and is
    skipped — no pre-dropping, so short transcripts keep their first record.
    """
    for raw in blob.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


# -------------------------------------------------------------- chat blocks
#
# The chat view gets the transcript as ordered typed blocks per message —
# text, thinking, tool calls with their results paired back in — instead of a
# flattened text+toolnames digest. The client's renderer decides how each
# block looks; the parser's job is fidelity and bounding the payload.

CHAT_INPUT_STR_CAP = 6000  # per string in a tool input; diffs need old/new whole
CHAT_RESULT_CAP = 2500
CHAT_THINKING_CAP = 6000

_INTERNAL_PREFIXES = ("Caveat:", "[Request interrupted")
_CMD_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_CMD_OUT_RE = re.compile(r"<local-command-stdout>(.*?)</local-command-stdout>", re.S)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (+{len(text) - limit} more chars)"


def _trim_strings(value, limit: int = CHAT_INPUT_STR_CAP):
    """A tool input with every string capped — structure kept for the client."""
    if isinstance(value, str):
        return _cap(value, limit)
    if isinstance(value, list):
        return [_trim_strings(v, limit) for v in value[:50]]
    if isinstance(value, dict):
        return {k: _trim_strings(v, limit) for k, v in list(value.items())[:40]}
    return value


def _result_text(content) -> str:
    """The text of a tool_result whose content is a string or a block list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b["text"]) for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))
    return "" if content is None else json.dumps(content)[:500]


def _image_src(block: dict) -> str | None:
    src = block.get("source") or {}
    if src.get("type") == "base64" and isinstance(src.get("data"), str):
        return f"data:{src.get('media_type') or 'image/png'};base64,{src['data']}"
    return None


def _user_str_block(text: str, out: list[dict]) -> dict | None:
    """A user record's string content as a block, or None when it is noise.

    Slash-command payloads and their stdout arrive wrapped in tag markup and
    become slim command rows (stdout folds onto the command it follows);
    injected context — reminders, caveats, interruption banners, skill
    bodies — is machinery the user never typed and stays hidden.
    """
    m = _CMD_NAME_RE.search(text)
    if m:
        args = _CMD_ARGS_RE.search(text)
        return {"t": "command", "name": m.group(1).strip(),
                "args": args.group(1).strip() if args else ""}
    m = _CMD_OUT_RE.search(text)
    if m:
        body = m.group(1).strip()
        for msg in reversed(out[-3:]):  # its command sits a record or two back
            last = msg["blocks"][-1] if msg.get("blocks") else None
            if last and last.get("t") == "command" and "output" not in last:
                if body:
                    last["output"] = _cap(body, CHAT_RESULT_CAP)
                return None
        return {"t": "command", "name": "", "output": _cap(body, CHAT_RESULT_CAP)} if body else None
    if text.startswith("<") or text.startswith(_INTERNAL_PREFIXES):
        return None
    return {"t": "text", "text": text}


def _prompt_row(rec: dict) -> bool:
    """True for a row the user typed, not a tool result or injected note."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") in ("text", "image") for b in content)
    return isinstance(content, str) and bool(content)


def drop_superseded_branches(records: list[dict]) -> list[dict]:
    """Without the rows of prompts that were replaced by an edit or rewind.

    Editing a message makes Claude resume from an earlier point and append
    the replacement, so two prompts end up sharing one parentUuid and a flat
    read shows both. Only sibling *prompts* mark a fork — tool results parent
    freely under one assistant turn — and the file is append-only, so the
    last sibling written is the live one; the others' subtrees are dead.
    """
    siblings: dict[str, list[dict]] = {}
    for rec in records:
        if isinstance(rec.get("parentUuid"), str) and _prompt_row(rec):
            siblings.setdefault(rec["parentUuid"], []).append(rec)
    dead: set[str] = set()
    for group in siblings.values():
        for rec in group[:-1]:
            if isinstance(rec.get("uuid"), str):
                dead.add(rec["uuid"])
    if not dead:
        return records
    for rec in records:  # append order propagates each root to its subtree
        if rec.get("parentUuid") in dead and isinstance(rec.get("uuid"), str):
            dead.add(rec["uuid"])
    return [r for r in records if r.get("uuid") not in dead]


def parse_claude_messages(blob: bytes, limit: int = CHAT_LIMIT) -> list[dict]:
    """The conversation as the chat view shows it, oldest first.

    Messages are {"role", "ts", "blocks"} with blocks in transcript order.
    Consecutive assistant records are one logical turn (transcripts write one
    record per streamed block); tool results are folded onto the tool_use
    that produced them, so a tool block still `pending` genuinely never got
    an answer — running if it is the newest, interrupted otherwise.
    """
    records = drop_superseded_branches(list(_iter_records(blob)))
    out: list[dict] = []
    by_id: dict[str, dict] = {}  # tool_use_id -> its tool block

    def assistant_turn(ts):
        if out and out[-1]["role"] == "assistant":
            out[-1]["ts"] = ts or out[-1].get("ts")
            return out[-1]
        entry = {"role": "assistant", "ts": ts, "blocks": []}
        out.append(entry)
        return entry

    for rec in records:
        if rec.get("isSidechain"):
            continue
        rtype = rec.get("type")
        ts = rec.get("timestamp")
        if rtype == "user":
            content = (rec.get("message") or {}).get("content")
            if rec.get("isCompactSummary"):
                text = content if isinstance(content, str) else _result_text(content)
                out.append({"role": "system", "ts": ts,
                            "blocks": [{"t": "compact", "text": text}]})
                continue
            if rec.get("isMeta"):
                continue
            if isinstance(content, str) and content:
                block = _user_str_block(content, out)
                if block:
                    out.append({"role": "user", "ts": ts, "blocks": [block]})
            elif isinstance(content, list):
                blocks: list[dict] = []
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    btype = b.get("type")
                    if btype == "tool_result":
                        tool = by_id.pop(b.get("tool_use_id") or "", None)
                        if tool is not None:
                            tool["result"] = _cap(_result_text(b.get("content")), CHAT_RESULT_CAP)
                            if b.get("is_error"):
                                tool["error"] = True
                            tool.pop("pending", None)
                    elif btype == "text" and b.get("text"):
                        block = _user_str_block(str(b["text"]), out)
                        if block:
                            blocks.append(block)
                    elif btype == "image":
                        src = _image_src(b)
                        if src:
                            blocks.append({"t": "image", "src": src})
                if blocks:
                    out.append({"role": "user", "ts": ts, "blocks": blocks})
        elif rtype == "assistant":
            msg = rec.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            fresh: list[dict] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text" and b.get("text"):
                    fresh.append({"t": "text", "text": str(b["text"])})
                elif btype == "thinking" and b.get("thinking"):
                    fresh.append({"t": "thinking",
                                  "text": _cap(str(b["thinking"]), CHAT_THINKING_CAP)})
                elif btype == "tool_use":
                    tool = {"t": "tool", "name": b.get("name") or "tool",
                            "input": (_trim_strings(b["input"])
                                      if isinstance(b.get("input"), dict) else {}),
                            "pending": True}
                    if isinstance(b.get("id"), str):
                        by_id[b["id"]] = tool
                    fresh.append(tool)
            if not fresh:
                continue
            entry = assistant_turn(ts)
            for block in fresh:
                last = entry["blocks"][-1] if entry["blocks"] else None
                if (block["t"] in ("text", "thinking") and last
                        and last["t"] == block["t"]):
                    last["text"] += "\n\n" + block["text"]
                else:
                    entry["blocks"].append(block)
            if msg.get("model"):
                entry["model"] = msg["model"]
    return out[-limit:]


def claude_session_meta(blob: bytes) -> dict:
    """Title, model and newest context usage for the chat header, newest wins.

    Context is the prompt side of the newest assistant turn's usage —
    input + cache read + cache creation — i.e. how full the window is.
    Interrupted turns write all-zero usage rows; those don't count.
    """
    meta: dict = {}
    for rec in reversed(list(_iter_records(blob))):
        rtype = rec.get("type")
        if "title" not in meta:
            if rtype == "ai-title" and rec.get("aiTitle"):
                meta["title"] = _snip(str(rec["aiTitle"]), 120)
            elif rtype == "custom-title" and rec.get("customTitle"):
                meta["title"] = _snip(str(rec["customTitle"]), 120)
        if rtype == "assistant" and not rec.get("isSidechain"):
            msg = rec.get("message") or {}
            if "model" not in meta and msg.get("model"):
                meta["model"] = msg["model"]
            usage = msg.get("usage")
            if "context" not in meta and isinstance(usage, dict):
                total = 0
                for key in ("input_tokens", "cache_read_input_tokens",
                            "cache_creation_input_tokens"):
                    v = usage.get(key)
                    total += v if isinstance(v, int) else 0
                if total:
                    meta["context"] = total
        if len(meta) == 3:
            break
    return meta


# ------------------------------------------------------------------ codex


# A sub-thread rollout (spawned agent, review) carries its parent's thread id
# in session_meta; the pane's own interactive session never does.
_SUBTHREAD_RE = re.compile(rb'"parent_thread_id"\s*:\s*"')


def _codex_head_cwd(head: bytes) -> str | None:
    """The cwd from a rollout's session_meta header. The meta line embeds the
    agent's full base instructions and easily exceeds any sane head read, so
    a truncated line is the norm — but cwd sits in the first few hundred
    bytes, so fall back to plucking it straight from the bytes."""
    try:
        meta = json.loads(head.split(b"\n", 1)[0])
        return (meta.get("payload") or {}).get("cwd")
    except json.JSONDecodeError:
        m = re.search(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"', head)
        if not m:
            return None
        try:
            return json.loads(b'"' + m.group(1) + b'"')
        except json.JSONDecodeError:
            return None


def _codex_tool(payload: dict) -> dict:
    args = payload.get("arguments") or payload.get("input") or ""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"command": args}
    return {
        "name": payload.get("name") or "tool",
        "detail": _tool_detail(args if isinstance(args, dict) else {}),
    }


def _codex_output(output) -> tuple[str, bool]:
    """(text, is_error) from a codex tool output, which wraps shell output in
    a JSON envelope carrying exit metadata — sometimes doubly serialized."""
    if isinstance(output, list):  # newer CLIs write a list of text parts
        output = "\n".join(str(p.get("text")) for p in output
                           if isinstance(p, dict) and p.get("text"))
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return output, False
        if not isinstance(parsed, dict):
            return output, False
        output = parsed
    if isinstance(output, dict):
        meta = output.get("metadata") or {}
        text = output.get("output") or output.get("content") or ""
        return str(text), bool(meta.get("exit_code"))
    return "", False


def _codex_texts(content) -> str:
    """Joined text parts from an item's content list, whatever the casing —
    codex writes `text`, `Text`, `input_text` and `output_text` variants."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n\n".join(str(part.get("text")) for part in content
                       if isinstance(part, dict) and part.get("text"))


def _codex_command(item: dict) -> str:
    """The human-readable command of a CommandExecution item: the parsed form
    when codex provides one, else the argv with its `zsh -lc` wrapper cut."""
    parsed = item.get("parsed_cmd")
    if isinstance(parsed, list):
        cmds = [str(p["cmd"]) for p in parsed if isinstance(p, dict) and p.get("cmd")]
        if cmds:
            return "; ".join(cmds)
    argv = item.get("command")
    if isinstance(argv, list):
        if len(argv) == 3 and str(argv[1]) in ("-lc", "-c"):
            return str(argv[2])
        return " ".join(str(a) for a in argv)
    return str(argv or "")


def _codex_item_message(item: dict, ts) -> dict | None:
    """One item_completed entry as a chat message — modern codex rollouts
    (cli >= ~0.150) journal the conversation as typed items, not events.

    Commands and file changes are normalized onto the claude tool names, so
    the client renders one vocabulary: a shell run is a Bash row, an edit is
    an Edit/Write diff, whichever agent produced it.
    """
    itype = item.get("type")
    if itype == "UserMessage":
        text = _codex_texts(item.get("content"))
        if not text or text.startswith("<"):  # injected context wrappers
            return None
        return {"role": "user", "ts": ts, "blocks": [{"t": "text", "text": text}]}
    if itype == "AgentMessage":
        text = _codex_texts(item.get("content"))
        if not text:
            return None
        return {"role": "assistant", "ts": ts, "blocks": [{"t": "text", "text": text}]}
    if itype == "Reasoning":
        text = _codex_texts(item.get("summary_text") or item.get("raw_content"))
        if not text:  # server-side reasoning is usually encrypted — nothing to show
            return None
        return {"role": "assistant", "ts": ts,
                "blocks": [{"t": "thinking", "text": _cap(text, CHAT_THINKING_CAP)}]}
    if itype == "CommandExecution":
        output = item.get("aggregated_output") or "\n".join(
            filter(None, [str(item.get("stdout") or ""), str(item.get("stderr") or "")]))
        tool = {"t": "tool", "name": "Bash",
                "input": {"command": _cap(_codex_command(item), CHAT_INPUT_STR_CAP)},
                "result": _cap(str(output), CHAT_RESULT_CAP)}
        if item.get("status") == "failed" or item.get("exit_code"):
            tool["error"] = True
        if item.get("status") == "in_progress":
            tool["pending"] = True
        return {"role": "assistant", "ts": ts, "blocks": [tool]}
    if itype == "FileChange":
        blocks = []
        for path, change in (item.get("changes") or {}).items():
            if not isinstance(change, dict):
                continue
            if change.get("type") == "add" and isinstance(change.get("content"), str):
                blocks.append({"t": "tool", "name": "Write",
                               "input": {"file_path": path,
                                         "content": _cap(change["content"], CHAT_INPUT_STR_CAP)},
                               "result": ""})
            else:
                inp: dict = {"file_path": path}
                if isinstance(change.get("unified_diff"), str):
                    inp["unified_diff"] = _cap(change["unified_diff"], CHAT_INPUT_STR_CAP)
                if change.get("move_path"):
                    inp["move_path"] = str(change["move_path"])
                blocks.append({"t": "tool", "name": "Edit", "input": inp, "result": ""})
        if not blocks:
            return None
        return {"role": "assistant", "ts": ts, "blocks": blocks}
    return None


def parse_codex_messages(blob: bytes, limit: int = CHAT_LIMIT) -> list[dict]:
    """Codex CLI rollout files, normalized onto the claude block shape.

    Modern rollouts journal the conversation as `item_completed` items —
    messages, reasoning, command runs with their output attached, file
    changes — and everything else (developer messages, raw tool calls) is
    machinery that would duplicate or bury them. Older rollouts have no
    items; they get the event/function-call pairing instead.
    """
    records = list(_iter_records(blob))
    items = [(rec.get("timestamp"), (rec.get("payload") or {}).get("item"))
             for rec in records
             if rec.get("type") == "event_msg"
             and (rec.get("payload") or {}).get("type") == "item_completed"]

    out: list[dict] = []

    def fold(msg: dict) -> None:
        last = out[-1] if out else None
        if last and last["role"] == msg["role"] == "assistant":
            blocks = last["blocks"]
            for block in msg["blocks"]:
                if (block["t"] in ("text", "thinking") and blocks
                        and blocks[-1]["t"] == block["t"]):
                    blocks[-1]["text"] += "\n\n" + block["text"]
                else:
                    blocks.append(block)
            last["ts"] = msg["ts"] or last.get("ts")
        else:
            out.append(msg)

    if items:
        for ts, item in items:
            if isinstance(item, dict):
                msg = _codex_item_message(item, ts)
                if msg:
                    fold(msg)
        return out[-limit:]

    by_call: dict[str, dict] = {}  # call_id -> its tool block
    for rec in records:
        rtype = rec.get("type")
        p = rec.get("payload") or {}
        ts = rec.get("timestamp")
        ptype = p.get("type")
        if rtype == "event_msg":
            if ptype == "user_message" and p.get("message"):
                out.append({"role": "user", "ts": ts,
                            "blocks": [{"t": "text", "text": str(p["message"])}]})
            elif ptype == "agent_message" and p.get("message"):
                fold({"role": "assistant", "ts": ts,
                      "blocks": [{"t": "text", "text": str(p["message"])}]})
            elif ptype == "agent_reasoning" and p.get("text"):
                fold({"role": "assistant", "ts": ts,
                      "blocks": [{"t": "thinking",
                                  "text": _cap(str(p["text"]), CHAT_THINKING_CAP)}]})
        elif rtype == "response_item":
            if ptype in ("function_call", "custom_tool_call"):
                args = p.get("arguments") or p.get("input") or ""
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"command": args}
                tool = {"t": "tool", "name": p.get("name") or "tool",
                        "input": _trim_strings(args) if isinstance(args, dict) else {},
                        "pending": True}
                if isinstance(p.get("call_id"), str):
                    by_call[p["call_id"]] = tool
                fold({"role": "assistant", "ts": ts, "blocks": [tool]})
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                tool = by_call.pop(p.get("call_id") or "", None)
                if tool is not None:
                    text, error = _codex_output(p.get("output"))
                    tool["result"] = _cap(text, CHAT_RESULT_CAP)
                    if error:
                        tool["error"] = True
                    tool.pop("pending", None)
    return out[-limit:]


def codex_session_meta(blob: bytes) -> dict:
    """Model and newest context usage for the chat header — codex reports a
    running token_count event with the window size alongside, and the model
    rides on each turn's turn_context."""
    meta: dict = {}
    for rec in reversed(list(_iter_records(blob))):
        rtype = rec.get("type")
        p = rec.get("payload") or {}
        if "model" not in meta:
            if rtype == "turn_context" and p.get("model"):
                meta["model"] = str(p["model"])
            elif (rtype == "event_msg" and p.get("type") == "thread_settings_applied"
                  and (p.get("thread_settings") or {}).get("model")):
                meta["model"] = str(p["thread_settings"]["model"])
        if ("context" not in meta and rtype == "event_msg"
                and p.get("type") == "token_count"):
            info = p.get("info") or p
            usage = info.get("last_token_usage") or info.get("total_token_usage") or {}
            total = usage.get("total_tokens")
            if not isinstance(total, int):
                total = sum(v for v in (usage.get("input_tokens"), usage.get("output_tokens"))
                            if isinstance(v, int))
            if total:
                meta["context"] = total
            window = info.get("model_context_window")
            if isinstance(window, int) and window:
                meta["window"] = window
        if "model" in meta and "context" in meta:
            break
    return meta


def parse_codex_tail(blob: bytes) -> Summary:
    """Card summary for a codex rollout, same shape as the claude one."""
    out = Summary()
    open_tool: dict | None = None
    answered: set[str] = set()  # call_ids whose outputs we've already passed
    for rec in reversed(list(_iter_records(blob))):
        rtype = rec.get("type")
        p = rec.get("payload") or {}
        if out.ts is None and rec.get("timestamp"):
            out.ts = rec["timestamp"]
        if rtype == "event_msg":
            ptype = p.get("type")
            if ptype == "user_message" and out.last_prompt is None and p.get("message"):
                out.last_prompt = _snip(str(p["message"]))
            elif ptype == "agent_message" and out.last_assistant is None and p.get("message"):
                out.last_assistant = _snip(str(p["message"]))
            elif ptype == "item_completed":
                item = p.get("item") or {}
                itype = item.get("type")
                if itype == "UserMessage" and out.last_prompt is None:
                    text = _codex_texts(item.get("content"))
                    if text and not text.startswith("<"):
                        out.last_prompt = _snip(text)
                elif itype == "AgentMessage" and out.last_assistant is None:
                    text = _codex_texts(item.get("content"))
                    if text:
                        out.last_assistant = _snip(text)
                elif (itype == "CommandExecution" and open_tool is None
                      and out.last_assistant is None
                      and item.get("status") == "in_progress"):
                    open_tool = {"name": "Bash",
                                 "detail": _snip(_codex_command(item), 120)}
        elif rtype == "turn_context":
            if out.model is None and p.get("model"):
                out.model = str(p["model"])
        elif rtype == "response_item":
            ptype = p.get("type")
            if ptype in ("function_call_output", "custom_tool_call_output"):
                answered.add(p.get("call_id") or "")
            elif (ptype in ("function_call", "custom_tool_call")
                  and open_tool is None and out.last_assistant is None
                  and p.get("call_id") not in answered):
                open_tool = _codex_tool(p)
        elif rtype == "session_meta" and out.model is None:
            out.model = (p.get("model_provider") or None)
        if out.last_prompt and out.last_assistant:
            break
    out.current_tool = open_tool
    return out


_PATH_KEYS = ("file_path", "notebook_path", "path")


def mentioned_files(blob: bytes, cap: int = FILES_CAP) -> list[str]:
    """Absolute paths the agent touched via tools, newest first, deduped."""
    seen: dict[str, None] = {}
    for rec in _iter_records(blob):
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            inp = block.get("input")
            if not isinstance(inp, dict):
                continue
            for key in _PATH_KEYS:
                v = inp.get(key)
                if isinstance(v, str) and v.startswith("/"):
                    seen.pop(v, None)  # re-mention moves it to the newest slot
                    seen[v] = None
    return list(reversed(list(seen)))[:cap]


# Codex touches files through shell commands, not structured tool inputs, so
# references are scraped from the command text itself: apply_patch headers,
# redirect targets, and file:// URLs are explicit, everything else is "token
# that ends in an extension". Relative tokens are resolved against the session
# cwd and against the directories of files being written — markdown links in
# written content are relative to the linking file, not the cwd. Candidates
# are heuristic by nature — the caller stats them and keeps only what exists.
_PATCH_FILE_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: ([^\\\"\n]+)")
_REDIRECT_RE = re.compile(r">>?\s*([\w./~-]+)")
_FILE_URL_RE = re.compile(r"file://(/[\w./~-]+)")
_PATH_RUN_RE = re.compile(r"[\w./~-]+")
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _resolve(token: str, base: str | None) -> str | None:
    token = token.strip().rstrip(".")
    if token.startswith("~/"):
        token = token[2:]  # backends resolve home-relative paths
    elif not token.startswith("/"):
        if not base or token.startswith("~"):
            return None
        token = f"{base}/{token}"
    path = posixpath.normpath(token)
    if ":" in path or ".." in path.split("/") or path in ("/", "."):
        return None
    return path


def codex_mentioned_files(blob: bytes, cwd: str | None) -> list[str]:
    """Path candidates from a codex rollout's tool calls, newest first."""
    seen: dict[str, None] = {}

    def add(token: str, base: str | None) -> None:
        path = _resolve(token, base)
        if path is None:
            return
        seen.pop(path, None)  # re-mention moves it to the newest slot
        seen[path] = None

    def note_base(bases: list[str], token: str) -> None:
        target = _resolve(token, cwd)
        if target is not None:
            d = posixpath.dirname(target) or "."
            if d not in bases and len(bases) < 4:  # bound the fan-out
                bases.append(d)

    for rec in _iter_records(blob):
        if rec.get("type") != "response_item":
            continue
        p = rec.get("payload") or {}
        if p.get("type") not in ("function_call", "custom_tool_call"):
            continue
        text = p.get("arguments") or p.get("input") or ""
        if not isinstance(text, str):
            continue
        bases: list[str] = [cwd] if cwd else []
        for m in _PATCH_FILE_RE.finditer(text):
            add(m.group(1), cwd)
            note_base(bases, m.group(1))
        for m in _REDIRECT_RE.finditer(text):  # heredoc/redirect writes
            if _EXT_RE.search(m.group(1)):
                note_base(bases, m.group(1))
        for m in _FILE_URL_RE.finditer(text):
            add(m.group(1), None)
        for m in _PATH_RUN_RE.finditer(text):
            run = m.group(0).rstrip(".")
            if not _EXT_RE.search(run):
                continue
            prev = text[m.start() - 1] if m.start() else " "
            if prev in "!:":  # rg's negated globs, URL remainders
                continue
            nxt = text[m.end():m.end() + 1]
            if nxt == "(":  # json.loads(...) — a method call, not a file
                continue
            for base in bases or [None]:
                add(run, base)
    return list(reversed(list(seen)))


async def existing_files(fs, candidates: list[str], cap: int = FILES_CAP) -> list[str]:
    """The candidates that actually stat on the host, order kept, capped."""

    async def probe(path: str) -> str | None:
        try:
            await fs.stat(path)
            return path
        except Exception:  # noqa: BLE001 - a candidate that doesn't exist
            return None

    hits = await asyncio.gather(*(probe(p) for p in candidates[:400]))
    return [p for p in hits if p][:cap]


class TranscriptPeek:
    """Cached tail-parses, keyed by (host, path) and invalidated by stat."""

    def __init__(self, backend_for):
        self.backend_for = backend_for  # host -> LocalFs | SftpFs
        self.cache: dict[tuple[str, str], tuple[tuple, Summary]] = {}
        self.chat_cache: dict[tuple[str, str], tuple[tuple, dict]] = {}
        self.codex_cache: dict[tuple[str, str], str] = {}  # (host, cwd|id) -> path

    CODEX_ROOT = ".codex/sessions"
    CODEX_PROBES = 25  # header reads per search; sessions live in date dirs

    async def _codex_find(self, host: str, cwd: str | None = None,
                          session_id: str | None = None) -> str | None:
        """Walk ~/.codex/sessions newest-first for the rollout matching a
        session id (by filename) or a working directory (by the session_meta
        header on line one). A cwd guess wants the pane's interactive thread:
        most recently written wins, and sub-thread rollouts (spawned agents,
        reviews — marked by parent_thread_id) never match. Bounded probing;
        the hit is cached and re-validated by stat in the caller."""
        key = (host, session_id or cwd or "")
        cached = self.codex_cache.get(key)
        fs = self.backend_for(host)
        if cached:
            try:
                await fs.stat(cached)
                return cached
            except Exception:  # noqa: BLE001 - rotated away; re-probe
                self.codex_cache.pop(key, None)

        async def subdirs(path):
            try:
                return sorted((e["name"] for e in await fs.listdir(path) if e["dir"]),
                              reverse=True)
            except Exception:  # noqa: BLE001 - no codex on this host
                return []

        probes = 0
        for year in (await subdirs(self.CODEX_ROOT))[:2]:
            for month in await subdirs(f"{self.CODEX_ROOT}/{year}"):
                for day in await subdirs(f"{self.CODEX_ROOT}/{year}/{month}"):
                    dirp = f"{self.CODEX_ROOT}/{year}/{month}/{day}"
                    try:
                        files = [e for e in await fs.listdir(dirp)
                                 if not e["dir"] and e["name"].endswith(".jsonl")]
                    except Exception:  # noqa: BLE001
                        continue
                    files.sort(key=lambda e: (e.get("mtime") or 0, e["name"]),
                               reverse=True)
                    for entry in files:
                        path = f"{dirp}/{entry['name']}"
                        if session_id:
                            if session_id in entry["name"]:
                                self.codex_cache[key] = path
                                return path
                            continue
                        probes += 1
                        if probes > self.CODEX_PROBES:
                            return None
                        try:
                            head = await fs.read_head(path, 4096)
                        except Exception:  # noqa: BLE001 - unreadable header
                            continue
                        if (_codex_head_cwd(head) == cwd
                                and not _SUBTHREAD_RE.search(head)):
                            self.codex_cache[key] = path
                            return path
        return None

    async def locate(self, host: str, pane: dict) -> tuple[str | None, str | None, bool, str]:
        """(path, reason-if-none, guessed, format). When herdr reports no
        exact agent session, fall back per agent: claude gets the newest
        transcript in its project directory for the pane's cwd, codex gets
        the rollout whose header cwd matches — right whenever one session
        works a checkout, which is the overwhelmingly common case."""
        sess = pane.get("agent_session") or {}
        agent = (sess.get("agent") or pane.get("agent") or "").lower()
        cwd = pane.get("cwd")
        fmt = "codex" if agent == "codex" else "claude"

        path, reason = resolve_path(pane)
        if path is not None:
            if ".codex/" in path:
                fmt = "codex"
            return path, None, False, fmt
        if sess.get("kind") == "id" and agent == "codex" and sess.get("value"):
            found = await self._codex_find(host, session_id=str(sess["value"]))
            if found:
                return found, None, False, "codex"

        if agent == "claude" and cwd:
            projdir = f".claude/projects/{munge_cwd(cwd)}"
            try:
                entries = await self.backend_for(host).listdir(projdir)
                logs = [e for e in entries if not e["dir"] and e["name"].endswith(".jsonl")]
            except Exception:  # noqa: BLE001 - no claude project dir on that host
                logs = []
            if logs:
                newest = max(logs, key=lambda e: e.get("mtime") or 0)
                return f"{projdir}/{newest['name']}", None, True, "claude"
            return None, f"no Claude transcripts found for {cwd}", False, fmt
        if agent == "codex" and cwd:
            found = await self._codex_find(host, cwd=cwd)
            if found:
                return found, None, True, "codex"
            return None, f"no codex session found for {cwd}", False, fmt
        if agent not in ("claude", "codex"):
            return None, f"chat is not supported for {agent or 'unknown'} agents yet", False, fmt
        return None, reason, False, fmt

    async def summary(self, host: str, pane: dict) -> dict:
        path, reason, guessed, fmt = await self.locate(host, pane)
        if path is None:
            return {"available": False, "reason": reason}
        fs = self.backend_for(host)
        try:
            stamp = await fs.stat(path)
        except Exception as e:  # noqa: BLE001 - a missing transcript is normal
            return {"available": False, "reason": f"transcript unreadable: {e}", "path": path}
        key = (host, path)
        cached = self.cache.get(key)
        if cached and cached[0] == stamp:
            summary = cached[1]
        else:
            try:
                blob = await fs.read_tail(path, TAIL_BYTES)
            except Exception as e:  # noqa: BLE001
                return {"available": False, "reason": f"transcript unreadable: {e}", "path": path}
            summary = parse_codex_tail(blob) if fmt == "codex" else parse_claude_tail(blob)
            self.cache[key] = (stamp, summary)
            if len(self.cache) > 512:  # panes come and go; keep it bounded
                self.cache.pop(next(iter(self.cache)))
        return {
            "available": True,
            "path": path,
            "guessed": guessed,
            "mtime": stamp[0],
            "size": stamp[1],
            **{k: v for k, v in asdict(summary).items() if v is not None},
        }

    async def messages(self, host: str, pane: dict) -> dict:
        """The chat view's payload: message list + files the agent touched."""
        path, reason, guessed, fmt = await self.locate(host, pane)
        if path is None:
            return {"available": False, "reason": reason}
        fs = self.backend_for(host)
        try:
            stamp = await fs.stat(path)
        except Exception as e:  # noqa: BLE001 - a missing transcript is normal
            return {"available": False, "reason": f"transcript unreadable: {e}", "path": path}
        key = (host, path)
        cached = self.chat_cache.get(key)
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            blob = await fs.read_tail(path, CHAT_TAIL_BYTES)
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": f"transcript unreadable: {e}", "path": path}
        if fmt == "codex":
            cwd = pane.get("cwd")
            if not cwd:  # the tail has no session_meta; the head does
                try:
                    cwd = _codex_head_cwd(await fs.read_head(path, 4096))
                except Exception:  # noqa: BLE001 - cwd is best-effort
                    cwd = None
            files = await existing_files(fs, codex_mentioned_files(blob, cwd))
            session = codex_session_meta(blob)
        else:
            files = mentioned_files(blob)
            session = claude_session_meta(blob)
        payload = {
            "available": True,
            "path": path,
            "guessed": guessed,
            "format": fmt,
            "mtime": stamp[0],
            "size": stamp[1],
            "session": session,
            "messages": (parse_codex_messages(blob) if fmt == "codex"
                         else parse_claude_messages(blob)),
            "files": files,
        }
        self.chat_cache[key] = (stamp, payload)
        if len(self.chat_cache) > 64:  # chat payloads are chunky; keep few
            self.chat_cache.pop(next(iter(self.chat_cache)))
        return payload

    async def enrich(self, poller) -> None:
        """Attach transcript summaries to a fresh poll's agent panes."""
        panes = (poller.state.get("data") or {}).get("panes") or []
        for pane in panes:
            if not pane.get("is_agent"):
                continue
            try:
                result = await self.summary(poller.name, pane)
                if result.get("available"):
                    pane["transcript"] = result
            except Exception as e:  # noqa: BLE001 - never fail the poll
                log.debug("%s: transcript enrich failed: %s", poller.name, e)
