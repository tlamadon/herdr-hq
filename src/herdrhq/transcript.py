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

import json
import logging
import re
from dataclasses import asdict, dataclass

log = logging.getLogger("herdrhq.transcript")

TAIL_BYTES = 256 * 1024
CHAT_TAIL_BYTES = 512 * 1024
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
    for key in ("command", "description", "file_path", "path", "pattern", "prompt", "url", "query"):
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


def parse_claude_messages(blob: bytes, limit: int = CHAT_LIMIT) -> list[dict]:
    """The conversation as the chat view shows it, oldest first.

    Sidechains, meta records, tool results and machinery types are skipped;
    what remains is what the user asked, what the agent said (markdown, left
    intact for the client to render), and which tools it called in between.
    """
    out: list[dict] = []
    for rec in _iter_records(blob):
        if rec.get("isSidechain") or rec.get("isMeta"):
            continue
        rtype = rec.get("type")
        ts = rec.get("timestamp")
        if rtype == "user":
            content = (rec.get("message") or {}).get("content")
            text = None
            if isinstance(content, str):
                if content and not content.startswith("<"):
                    text = content
            elif isinstance(content, list):
                parts = [b.get("text") for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
                if parts:
                    text = "\n\n".join(parts)
            if text:
                out.append({"role": "user", "text": text, "ts": ts})
        elif rtype == "assistant":
            msg = rec.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            texts = []
            tools = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
                elif block.get("type") == "tool_use":
                    tools.append({
                        "name": block.get("name") or "tool",
                        "detail": _tool_detail(block.get("input")),
                    })
            if not texts and not tools:
                continue
            # transcripts write one record per streamed block, and tool results
            # sit between them as (skipped) user records — consecutive
            # assistant records are one logical turn, so merge them
            if out and out[-1]["role"] == "assistant":
                entry = out[-1]
            else:
                entry = {"role": "assistant", "ts": ts}
                out.append(entry)
            if texts:
                entry["text"] = "\n\n".join(filter(None, [entry.get("text"), *texts]))
            if tools:
                entry["tools"] = (entry.get("tools") or []) + tools
            if msg.get("model"):
                entry["model"] = msg["model"]
            entry["ts"] = ts or entry.get("ts")
    return out[-limit:]


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


class TranscriptPeek:
    """Cached tail-parses, keyed by (host, path) and invalidated by stat."""

    def __init__(self, backend_for):
        self.backend_for = backend_for  # host -> LocalFs | SftpFs
        self.cache: dict[tuple[str, str], tuple[tuple, Summary]] = {}
        self.chat_cache: dict[tuple[str, str], tuple[tuple, dict]] = {}

    async def locate(self, host: str, pane: dict) -> tuple[str | None, str | None, bool]:
        """(path, reason-if-none, guessed). When herdr reports no exact agent
        session, fall back to the newest transcript in the Claude project
        directory for the pane's cwd — right whenever one session works a
        checkout, which is the overwhelmingly common case."""
        path, reason = resolve_path(pane)
        if path is not None:
            return path, None, False
        agent = (pane.get("agent") or "").lower()
        cwd = pane.get("cwd")
        if agent != "claude" or not cwd:
            return None, reason, False
        projdir = f".claude/projects/{munge_cwd(cwd)}"
        try:
            entries = await self.backend_for(host).listdir(projdir)
        except Exception:  # noqa: BLE001 - no claude project dir on that host
            return None, f"{reason}; no transcripts found for {cwd}", False
        logs = [e for e in entries if not e["dir"] and e["name"].endswith(".jsonl")]
        if not logs:
            return None, f"{reason}; no transcripts found for {cwd}", False
        newest = max(logs, key=lambda e: e.get("mtime") or 0)
        return f"{projdir}/{newest['name']}", None, True

    async def summary(self, host: str, pane: dict) -> dict:
        path, reason, guessed = await self.locate(host, pane)
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
            summary = parse_claude_tail(blob)
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
        path, reason, guessed = await self.locate(host, pane)
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
        payload = {
            "available": True,
            "path": path,
            "guessed": guessed,
            "mtime": stamp[0],
            "size": stamp[1],
            "messages": parse_claude_messages(blob),
            "files": mentioned_files(blob),
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
