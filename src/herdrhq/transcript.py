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


def parse_codex_messages(blob: bytes, limit: int = CHAT_LIMIT) -> list[dict]:
    """Codex CLI rollout files: event_msg user/agent messages are the clean
    conversation; function calls attach to the running assistant turn."""
    out: list[dict] = []

    def assistant_turn(ts):
        if out and out[-1]["role"] == "assistant":
            return out[-1]
        entry = {"role": "assistant", "ts": ts}
        out.append(entry)
        return entry

    for rec in _iter_records(blob):
        rtype = rec.get("type")
        p = rec.get("payload") or {}
        ts = rec.get("timestamp")
        if rtype == "event_msg":
            ptype = p.get("type")
            if ptype == "user_message" and p.get("message"):
                out.append({"role": "user", "text": str(p["message"]), "ts": ts})
            elif ptype == "agent_message" and p.get("message"):
                entry = assistant_turn(ts)
                entry["text"] = "\n\n".join(filter(None, [entry.get("text"), str(p["message"])]))
                entry["ts"] = ts
        elif rtype == "response_item" and p.get("type") in ("function_call", "custom_tool_call"):
            entry = assistant_turn(ts)
            entry["tools"] = (entry.get("tools") or []) + [_codex_tool(p)]
    return out[-limit:]


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
        else:
            files = mentioned_files(blob)
        payload = {
            "available": True,
            "path": path,
            "guessed": guessed,
            "format": fmt,
            "mtime": stamp[0],
            "size": stamp[1],
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
