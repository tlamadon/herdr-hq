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
SNIP = 400  # transcripts hold whole essays; cards need a line


def munge_cwd(cwd: str) -> str:
    """Claude Code's project-directory name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def resolve_path(pane: dict) -> tuple[str | None, str | None]:
    """(remote transcript path, reason-if-none) for a pane's agent_session."""
    sess = pane.get("agent_session")
    if not sess:
        host_hint = "install the herdr integration on the host (herdr integration install claude)"
        return None, f"agent session not reported — {host_hint}"
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
            return f"~/.claude/projects/{munge_cwd(cwd)}/{value}.jsonl", None
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
    lines = blob.split(b"\n")
    if len(lines) > 1:
        lines = lines[1:]  # the first line is almost surely cut mid-record

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


class TranscriptPeek:
    """Cached tail-parses, keyed by (host, path) and invalidated by stat."""

    def __init__(self, backend_for):
        self.backend_for = backend_for  # host -> LocalFs | SftpFs
        self.cache: dict[tuple[str, str], tuple[tuple, Summary]] = {}

    async def summary(self, host: str, pane: dict) -> dict:
        path, reason = resolve_path(pane)
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
            "mtime": stamp[0],
            "size": stamp[1],
            **{k: v for k, v in asdict(summary).items() if v is not None},
        }

    async def enrich(self, poller) -> None:
        """Attach transcript summaries to a fresh poll's agent panes."""
        panes = (poller.state.get("data") or {}).get("panes") or []
        for pane in panes:
            if not pane.get("is_agent") or not pane.get("agent_session"):
                continue
            try:
                pane["transcript"] = await self.summary(poller.name, pane)
            except Exception as e:  # noqa: BLE001 - never fail the poll
                log.debug("%s: transcript enrich failed: %s", poller.name, e)
