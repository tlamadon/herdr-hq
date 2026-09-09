"""Agent-account usage probe: Claude + Codex usage-limit windows as one JSON blob.

Like collector.py, this must stay a SINGLE FILE that imports only the stdlib:
it is piped over ssh to a bare `python3 -` and nothing is installed remotely.
Prints exactly one JSON object to stdout, parseable even on failure.

Claude (fresh at probe time) -- two calls to the Claude Code CLI:

  * `claude auth status`                     -> account email + plan
  * `claude -p /usage --output-format json`  -> usage-limit windows, parsed out
    of the human-readable `result` text. No structured form exists and the
    format is not a documented API, so parse failures are reported, not fatal.

  The -p probe costs no quota (no model call) but records a throwaway session
  under ~/.claude/projects/<munged cwd>/, so it is pinned to a dedicated
  working directory -- keeping those sessions away from the transcript-locate
  fallback, which picks the newest log for a pane's cwd -- and stale ones are
  pruned.

Codex (as fresh as the last session) -- no CLI needed, read from disk:

  * ~/.codex/sessions/**.jsonl rollouts record `token_count` events carrying a
    `rate_limits` snapshot ({used_percent, window_minutes, resets_at}); the
    newest one is reported along with its timestamp (`as_of`).
  * ~/.codex/auth.json's id_token JWT payload names the account (email, plan).
    Only those identity claims are decoded -- the tokens themselves are never
    read into the output.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import json
import os
import re
import shutil
import subprocess
import sys
import time

SCHEMA = 1

CLAUDE_CANDIDATES = [
    "claude",
    "~/.claude/local/claude",
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.nix-profile/bin/claude",
    "/run/current-system/sw/bin/claude",
    "/nix/var/nix/profiles/default/bin/claude",
]

# throwaway sessions from the -p probe land under this cwd's project dir
WORKDIR = "~/.cache/herdr-hq/usage"

# "Current session: 9% used · resets Sep 9 at 6:09pm (America/Chicago)"
WINDOW_RE = re.compile(r"^Current\s+(.+?):\s+(\d+)%\s+used(?:.*?resets\s+(.+?))?\s*$")

CODEX_DIR = "~/.codex"
CODEX_SCAN_FILES = 10  # newest rollouts checked for a rate_limits snapshot

warnings: list[str] = []


def warn(msg: str) -> None:
    if msg not in warnings:
        warnings.append(msg)


# --------------------------------------------------------------------------
# claude
# --------------------------------------------------------------------------


def find_claude() -> str | None:
    env_bin = os.environ.get("CLAUDE_BIN_PATH")
    if env_bin and os.path.exists(env_bin):
        return env_bin
    for cand in CLAUDE_CANDIDATES:
        path = os.path.expanduser(cand)
        found = shutil.which(path) if os.sep not in path else (path if os.access(path, os.X_OK) else None)
        if found:
            return found
    return None


def run_json(argv: list[str], timeout: float, cwd: str | None = None) -> tuple[dict | None, str | None]:
    """Run a CLI command and parse its stdout as JSON; returns (data, error)."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return None, f"{os.path.basename(argv[0])} {argv[1] if len(argv) > 1 else ''} timed out".strip()
    except OSError as exc:
        return None, str(exc)
    out = (proc.stdout or "").strip()
    if not out:
        return None, (proc.stderr or "").strip()[:300] or f"no output (exit {proc.returncode})"
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None, f"unparseable output: {out[:300]}"
    if not isinstance(data, dict):
        return None, f"unexpected output: {out[:300]}"
    return data, None


def auth_account(binary: str, timeout: float) -> dict | None:
    """Account identity from `claude auth status` (None on CLIs without it)."""
    data, err = run_json([binary, "auth", "status"], timeout)
    if data is None:
        if err:
            warn(f"auth status: {err}")
        return None
    return {
        "email": data.get("email"),
        "plan": data.get("subscriptionType"),
        "logged_in": data.get("loggedIn"),
    }


def parse_windows(text: str) -> list[dict]:
    wins = []
    for line in text.splitlines():
        m = WINDOW_RE.match(line.strip())
        if m:
            wins.append({"label": m.group(1), "pct": int(m.group(2)), "resets": m.group(3)})
    return wins


def usage_windows(binary: str, timeout: float, workdir: str | None) -> tuple[list[dict], str | None]:
    """(windows, error) from the headless /usage probe."""
    data, err = run_json([binary, "-p", "/usage", "--output-format", "json"], timeout, cwd=workdir)
    if data is None:
        return [], err
    text = str(data.get("result") or "")
    if data.get("is_error"):
        return [], text[:300] or "claude reported an error"
    wins = parse_windows(text)
    if not wins:
        return [], f"could not parse /usage output: {text[:300]}"
    return wins, None


def prune_sessions(workdir: str, max_age_s: float = 86400.0) -> None:
    """Delete old throwaway session logs recorded for OUR workdir only."""
    munged = re.sub(r"[^A-Za-z0-9]", "-", workdir)  # Claude Code's project-dir name
    proj = os.path.join(os.path.expanduser("~/.claude/projects"), munged)
    try:
        entries = os.listdir(proj)
    except OSError:
        return
    cutoff = time.time() - max_age_s
    for name in entries:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(proj, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
        except OSError:
            pass


def build_claude(timeout: float) -> dict | None:
    """The claude section, or None when the binary isn't on this host."""
    binary = find_claude()
    if not binary:
        return None
    out: dict = {"bin": binary}
    account = auth_account(binary, min(timeout, 15.0))
    out["account"] = account
    if account and account.get("logged_in") is False:
        out["windows"] = []
        out["error"] = "not logged in"
        return out
    workdir: str | None = os.path.expanduser(WORKDIR)
    try:
        os.makedirs(workdir, exist_ok=True)
    except OSError as exc:
        warn(f"workdir: {exc}")
        workdir = None
    windows, err = usage_windows(binary, timeout, workdir)
    out["windows"] = windows
    if err:
        out["error"] = err
    if workdir:
        prune_sessions(workdir)
    return out


# --------------------------------------------------------------------------
# codex
# --------------------------------------------------------------------------


def _jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying -- identity claims only."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return claims if isinstance(claims, dict) else {}
    except Exception:  # noqa: BLE001 - malformed token means no identity
        return {}


def codex_account(codex_dir: str) -> dict | None:
    """Identity claims from auth.json's id_token; the tokens never leave here."""
    try:
        with open(os.path.join(codex_dir, "auth.json")) as fh:
            auth = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    claims = _jwt_claims((auth.get("tokens") or {}).get("id_token") or "")
    oai = claims.get("https://api.openai.com/auth") or {}
    email = claims.get("email")
    plan = oai.get("chatgpt_plan_type")
    if not email and not plan:
        return None
    return {"email": email, "plan": plan}


def _iso_epoch(ts: str | None) -> float | None:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", ts or "")
    if not m:
        return None
    return calendar.timegm(tuple(int(g) for g in m.groups()) + (0, 0, 0))


def codex_rate_line(sessions_dir: str, max_files: int = CODEX_SCAN_FILES):
    """(rate_limits, line_timestamp, file_mtime) from the newest rollout carrying one."""
    files = []
    for root, _dirs, names in os.walk(sessions_dir):
        for name in names:
            if name.endswith(".jsonl"):
                path = os.path.join(root, name)
                try:
                    files.append((os.path.getmtime(path), path))
                except OSError:
                    pass
    for mtime, path in sorted(files, reverse=True)[:max_files]:
        last = None
        try:
            with open(path, errors="replace") as fh:
                for line in fh:
                    if '"rate_limits"' in line:
                        last = line
        except OSError:
            continue
        if not last:
            continue
        try:
            obj = json.loads(last)
        except json.JSONDecodeError:
            continue
        rl = (obj.get("payload") or {}).get("rate_limits")
        if isinstance(rl, dict):
            return rl, obj.get("timestamp"), mtime
    return None, None, None


def codex_windows(rl: dict) -> list[dict]:
    wins = []
    for part in (rl.get("primary"), rl.get("secondary")):
        if not isinstance(part, dict) or part.get("used_percent") is None:
            continue
        minutes = int(part.get("window_minutes") or 0)
        if 0 < minutes <= 360:
            label = "session"
        elif minutes == 10080:
            label = "week"
        elif minutes and minutes % 1440 == 0:
            label = f"{minutes // 1440}d"
        else:
            label = f"{minutes // 60}h" if minutes else "?"
        wins.append({
            "label": label,
            "pct": round(float(part["used_percent"])),
            "resets_at": part.get("resets_at"),
        })
    return wins


def build_codex() -> dict | None:
    """The codex section, or None when ~/.codex doesn't exist on this host."""
    codex_dir = os.path.expanduser(CODEX_DIR)
    if not os.path.isdir(codex_dir):
        return None
    account = codex_account(codex_dir) or {}
    rl, ts, mtime = codex_rate_line(os.path.join(codex_dir, "sessions"))
    out: dict = {"windows": []}
    if rl is None:
        out["error"] = "no rate-limit snapshot in recent sessions"
    else:
        out["windows"] = codex_windows(rl)
        if not out["windows"]:
            out["error"] = "unrecognized rate-limit snapshot"
        if not account.get("plan") and rl.get("plan_type"):
            account["plan"] = rl["plan_type"]
        as_of = _iso_epoch(ts) or mtime
        if as_of:
            out["as_of"] = as_of
    out["account"] = account or None
    return out


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------


def build(timeout: float) -> dict:
    out: dict = {"schema": SCHEMA, "collected_at": time.time(), "warnings": warnings}
    claude = build_claude(timeout)
    if claude is not None:
        out["claude"] = claude
    codex = build_codex()
    if codex is not None:
        out["codex"] = codex
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--timeout",
        type=float,
        default=25.0,
        help="seconds allowed for the claude /usage probe (default: 25)",
    )
    ap.add_argument("--pretty", action="store_true", help="indent the JSON output")
    args = ap.parse_args()

    try:
        data = build(max(5.0, args.timeout))
    except Exception as exc:  # noqa: BLE001 - always emit parseable JSON
        json.dump({"schema": SCHEMA, "fatal": str(exc), "collected_at": time.time()}, sys.stdout)
        sys.stdout.write("\n")
        return 1

    json.dump(data, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
