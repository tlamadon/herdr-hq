#!/usr/bin/env python3
"""Collect herdr session state + machine/agent resource usage on the local host.

Prints a single JSON object to stdout. Standard library only, so it can be piped
to a remote interpreter over ssh without installing anything:

    ssh box python3 - --interval 0.7 < collector.py

Per-agent CPU/memory is attributed exactly: herdr's socket API reports the shell
pid backing each pane (`herdr pane process-info`), and every process in that
shell's descendant tree is charged to the pane's agent.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

SCHEMA = 1

HERDR_CANDIDATES = [
    "herdr",
    "/opt/homebrew/bin/herdr",
    "/usr/local/bin/herdr",
    "~/.local/bin/herdr",
    "~/.nix-profile/bin/herdr",
    "/run/current-system/sw/bin/herdr",
    "/nix/var/nix/profiles/default/bin/herdr",
]

IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"

warnings: list[str] = []


def warn(msg: str) -> None:
    if msg not in warnings:
        warnings.append(msg)


# --------------------------------------------------------------------------
# herdr
# --------------------------------------------------------------------------


def find_herdr() -> str | None:
    env_bin = os.environ.get("HERDR_BIN_PATH")
    if env_bin and os.path.exists(env_bin):
        return env_bin
    for cand in HERDR_CANDIDATES:
        path = os.path.expanduser(cand)
        found = shutil.which(path) if os.sep not in path else (path if os.access(path, os.X_OK) else None)
        if found:
            return found
    return None


def herdr_json(binary: str, args: list[str], timeout: float = 10.0):
    """Run a herdr CLI subcommand and return its `result` payload."""
    proc = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError((proc.stderr.strip() or "no output") + f" (herdr {' '.join(args)})")
    payload = json.loads(out)
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    return payload.get("result", payload)


def collect_herdr(binary: str) -> tuple[dict, dict]:
    """Return (snapshot, {pane_id: process_info})."""
    snapshot = herdr_json(binary, ["api", "snapshot"])["snapshot"]
    pane_ids = [p["pane_id"] for p in snapshot.get("panes", [])]

    def one(pane_id: str):
        try:
            return pane_id, herdr_json(binary, ["pane", "process-info", "--pane", pane_id])["process_info"]
        except Exception as exc:  # noqa: BLE001 - a single bad pane must not sink the poll
            warn(f"pane process-info failed for {pane_id}: {exc}")
            return pane_id, None

    procinfo: dict[str, dict] = {}
    if pane_ids:
        with ThreadPoolExecutor(max_workers=min(8, len(pane_ids))) as pool:
            for pane_id, info in pool.map(one, pane_ids):
                if info:
                    procinfo[pane_id] = info
    return snapshot, procinfo


# --------------------------------------------------------------------------
# process sampling
# --------------------------------------------------------------------------

_MAC_PS = "/bin/ps" if os.path.exists("/bin/ps") else "ps"
_TIME_RE = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")


def _parse_ps_time(raw: str) -> float:
    """Parse BSD ps TIME (`[dd-][hh:]mm:ss[.cc]`) into seconds."""
    m = _TIME_RE.match(raw.strip())
    if not m:
        return 0.0
    days, hours, mins, secs = m.groups()
    return (
        float(days or 0) * 86400.0
        + float(hours or 0) * 3600.0
        + float(mins) * 60.0
        + float(secs)
    )


def sample_linux(full: bool) -> dict[int, dict]:
    ticks = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    procs: dict[int, dict] = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                raw = fh.read().decode("utf-8", "replace")
            # comm is parenthesised and may itself contain spaces or ')'
            _, _, rest = raw.partition("(")
            comm, _, tail = rest.rpartition(")")
            fields = tail.split()
            ppid = int(fields[1])
            cpu = (int(fields[11]) + int(fields[12])) / ticks
            rss = int(fields[21]) * page
        except (OSError, ValueError, IndexError):
            continue
        rec = {"ppid": ppid, "cpu": cpu, "rss": rss, "name": comm}
        if full:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cmdline = fh.read().decode("utf-8", "replace").replace("\x00", " ").strip()
                rec["cmdline"] = cmdline or f"[{comm}]"
            except OSError:
                rec["cmdline"] = f"[{comm}]"
        procs[pid] = rec
    return procs


def sample_bsd(full: bool) -> dict[int, dict]:
    fmt = "pid=,ppid=,rss=,time=,comm=" if not full else "pid=,ppid=,rss=,time=,args="
    proc = subprocess.run(
        [_MAC_PS, "-axwwo", fmt],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0 and not proc.stdout:
        raise RuntimeError(f"ps failed: {proc.stderr.strip()}")
    procs: dict[int, dict] = {}
    for line in proc.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, rss_kb = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        argv0 = parts[4].split()[0] if parts[4].strip() else "?"
        rec = {
            "ppid": ppid,
            "cpu": _parse_ps_time(parts[3]),
            "rss": rss_kb * 1024,
            # login shells arrive as "-zsh"; show the shell, not the dash
            "name": os.path.basename(argv0).lstrip("-") or argv0,
        }
        if full:
            rec["cmdline"] = parts[4].strip()
        else:
            rec["name"] = parts[4].strip()
        procs[pid] = rec
    return procs


def sample(full: bool = True) -> dict[int, dict]:
    if IS_LINUX and os.path.isdir("/proc/1"):
        return sample_linux(full)
    return sample_bsd(full)


def sample_pair(interval: float) -> tuple[dict[int, dict], float]:
    """Two samples `interval` apart; returns the second annotated with cpu_pct."""
    first = sample(full=False)
    t0 = time.monotonic()
    time.sleep(interval)
    second = sample(full=True)
    elapsed = max(time.monotonic() - t0, 1e-6)
    for pid, rec in second.items():
        prev = first.get(pid)
        delta = rec["cpu"] - prev["cpu"] if prev else 0.0
        # a negative delta means the pid was recycled between samples
        rec["cpu_pct"] = max(0.0, delta) / elapsed * 100.0
    return second, elapsed


def children_map(procs: dict[int, dict]) -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for pid, rec in procs.items():
        kids.setdefault(rec["ppid"], []).append(pid)
    return kids


def descendants(root: int, kids: dict[int, list[int]]) -> list[int]:
    out, stack, seen = [], [root], {root}
    while stack:
        pid = stack.pop()
        out.append(pid)
        for child in kids.get(pid, ()):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return out


def usage_for(pids: list[int], procs: dict[int, dict], top: int = 4) -> dict:
    cpu = 0.0
    rss = 0
    rows = []
    for pid in pids:
        rec = procs.get(pid)
        if not rec:
            continue
        cpu += rec["cpu_pct"]
        rss += rec["rss"]
        rows.append(
            {
                "pid": pid,
                "name": rec["name"],
                "cmdline": rec.get("cmdline", rec["name"])[:160],
                "cpu_pct": round(rec["cpu_pct"], 1),
                "rss": rec["rss"],
                "cpu_s": round(rec["cpu"], 1),
            }
        )
    rows.sort(key=lambda r: (r["cpu_pct"], r["rss"]), reverse=True)
    return {
        "cpu_pct": round(cpu, 1),
        "rss": rss,
        "proc_count": len(rows),
        "procs": rows[:top],
    }


# --------------------------------------------------------------------------
# machine stats
# --------------------------------------------------------------------------


def sysctl(name: str) -> str | None:
    try:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def memory() -> tuple[int, int]:
    """(total_bytes, used_bytes)."""
    if IS_LINUX and os.path.exists("/proc/meminfo"):
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    info[key] = int(parts[0]) * 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return total, max(0, total - avail)

    total = int(sysctl("hw.memsize") or 0)
    used = 0
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        page_size = 4096
        head = re.search(r"page size of (\d+) bytes", out)
        if head:
            page_size = int(head.group(1))
        pages = {}
        for line in out.splitlines()[1:]:
            key, _, rest = line.partition(":")
            digits = rest.strip().rstrip(".")
            if digits.isdigit():
                pages[key.strip()] = int(digits)
        used = (
            pages.get("Pages active", 0)
            + pages.get("Pages wired down", 0)
            + pages.get("Pages occupied by compressor", 0)
        ) * page_size
    except (OSError, subprocess.SubprocessError, ValueError):
        warn("vm_stat unavailable; memory usage unknown")
    return total, used


# --------------------------------------------------------------------------
# listening sockets
# --------------------------------------------------------------------------


def _hex_ip(raw: str) -> str:
    """Decode the little-endian hex address /proc/net/tcp uses."""
    try:
        if len(raw) == 8:
            return ".".join(str(b) for b in reversed(bytes.fromhex(raw)))
        if len(raw) == 32:
            words = [bytes.fromhex(raw[i:i + 8])[::-1] for i in range(0, 32, 8)]
            return socket.inet_ntop(socket.AF_INET6, b"".join(words))
    except (ValueError, OSError):
        pass
    return raw


def scope_of(addr: str) -> str:
    if addr in ("0.0.0.0", "::", "*"):
        return "all"
    if addr.startswith("127.") or addr in ("::1", "localhost"):
        return "loopback"
    return "bound"


def listening_linux(pids: set[int]) -> dict[int, list[dict]]:
    """Match LISTEN sockets to pids via the socket inodes in /proc/<pid>/fd."""
    inodes: dict[str, tuple[str, int]] = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as fh:
                next(fh, None)  # header
                for line in fh:
                    f = line.split()
                    if len(f) < 10 or f[3] != "0A":  # 0A = TCP_LISTEN
                        continue
                    host, _, port = f[1].rpartition(":")
                    inodes[f[9]] = (_hex_ip(host), int(port, 16))
        except OSError:
            continue
    if not inodes:
        return {}

    out: dict[int, list[dict]] = {}
    for pid in pids:
        try:
            entries = os.scandir(f"/proc/{pid}/fd")
        except OSError:
            continue
        with entries:
            for fd in entries:
                try:
                    target = os.readlink(fd.path)
                except OSError:
                    continue
                if not target.startswith("socket:["):
                    continue
                found = inodes.get(target[8:-1])
                if found:
                    out.setdefault(pid, []).append(
                        {"addr": found[0], "port": found[1], "proto": "tcp"}
                    )
    return out


def listening_bsd(pids: set[int]) -> dict[int, list[dict]]:
    """macOS has no /proc; ask lsof, restricted to the pids we care about."""
    if not shutil.which("lsof") or not pids:
        return {}
    ordered = sorted(pids)
    out: dict[int, list[dict]] = {}
    for start in range(0, len(ordered), 100):  # keep the argument list sane
        chunk = ordered[start:start + 100]
        try:
            proc = subprocess.run(
                ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpfn",
                 "-a", "-p", ",".join(str(p) for p in chunk)],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return out
        pid = None
        for line in proc.stdout.splitlines():
            tag, value = line[:1], line[1:]
            if tag == "p":
                pid = int(value) if value.isdigit() else None
            elif tag == "n" and pid is not None:
                addr, _, port = value.rpartition(":")
                if not port.isdigit():
                    continue
                addr = addr.strip("[]") or "*"
                out.setdefault(pid, []).append(
                    {"addr": "0.0.0.0" if addr == "*" else addr, "port": int(port), "proto": "tcp"}
                )
    return out


def listening_ports(pids: set[int]) -> dict[int, list[dict]]:
    if not pids:
        return {}
    try:
        if IS_LINUX and os.path.isdir("/proc/1"):
            return listening_linux(pids)
        return listening_bsd(pids)
    except Exception as exc:  # noqa: BLE001 - ports are a nice-to-have
        warn(f"port detection failed: {exc}")
        return {}


def ports_for(pids: list[int], sockets: dict[int, list[dict]], procs: dict[int, dict]) -> list[dict]:
    """Distinct listening endpoints somewhere in one pane's process tree."""
    seen: dict[tuple[str, int], dict] = {}
    for pid in pids:
        for sock in sockets.get(pid, ()):
            key = (sock["addr"], sock["port"])
            if key in seen:
                continue
            seen[key] = {
                **sock,
                "scope": scope_of(sock["addr"]),
                "pid": pid,
                "process": procs.get(pid, {}).get("name", "?"),
            }
    return sorted(seen.values(), key=lambda s: (s["port"], s["addr"]))


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------


def git(cwd: str, args: list[str], timeout: float = 8.0) -> str | None:
    """Run a read-only git command in `cwd`; None if it isn't a repo or git fails.

    `--no-optional-locks` keeps us from touching index.lock, so polling can never
    collide with a git command the agent itself is running.
    """
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def parse_status_v2(raw: str) -> dict:
    """Parse `git status --porcelain=v2 --branch` into branch + working-tree counts."""
    info = {
        "branch": None,
        "detached": False,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "conflicts": 0,
    }
    for line in raw.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):].strip()
            info["detached"] = head == "(detached)"
            info["branch"] = None if info["detached"] else head
        elif line.startswith("# branch.upstream "):
            info["upstream"] = line[len("# branch.upstream "):].strip()
        elif line.startswith("# branch.ab "):
            for token in line[len("# branch.ab "):].split():
                if token.startswith("+"):
                    info["ahead"] = int(token[1:])
                elif token.startswith("-"):
                    info["behind"] = int(token[1:])
        elif line.startswith("? "):
            info["untracked"] += 1
        elif line.startswith("u "):
            info["conflicts"] += 1
        elif line[:2] in ("1 ", "2 "):
            xy = line.split(" ", 2)[1]
            if xy[0] != ".":
                info["staged"] += 1
            if len(xy) > 1 and xy[1] != ".":
                info["unstaged"] += 1
    info["dirty"] = bool(
        info["staged"] or info["unstaged"] or info["untracked"] or info["conflicts"]
    )
    return info


def git_info(cwd: str) -> dict | None:
    """Repo, branch/worktree and clean-vs-dirty state for the directory an agent sits in."""
    if not cwd or not os.path.isdir(cwd):
        return None

    paths = git(cwd, ["rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"])
    if not paths:
        return None
    lines = paths.strip().splitlines()
    if len(lines) < 3:
        return None
    toplevel, git_dir, common_dir = (os.path.realpath(os.path.join(cwd, p)) for p in lines[:3])

    status = git(cwd, ["status", "--porcelain=v2", "--branch"])
    info = parse_status_v2(status) if status is not None else {}

    # a linked worktree has its own gitdir under <main>/.git/worktrees/<name>
    linked = git_dir != common_dir
    main_repo = os.path.dirname(common_dir) if os.path.basename(common_dir) == ".git" else common_dir

    if status is None:
        warn(f"git status failed in {cwd}")

    return {
        "repo": main_repo,
        "repo_name": os.path.basename(main_repo) or main_repo,
        "toplevel": toplevel,
        "worktree": linked,
        "worktree_name": os.path.basename(toplevel) if linked else None,
        **info,
    }


def collect_git(cwds: list[str]) -> dict[str, dict]:
    """One lookup per distinct directory — several agents often share a repo."""
    unique = sorted({c for c in cwds if c})
    if not unique:
        return {}
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(unique))) as pool:
        for cwd, info in zip(unique, pool.map(git_info, unique)):
            if info:
                out[cwd] = info
    return out


def uptime_seconds() -> float | None:
    if os.path.exists("/proc/uptime"):
        try:
            with open("/proc/uptime") as fh:
                return float(fh.read().split()[0])
        except (OSError, ValueError):
            return None
    raw = sysctl("kern.boottime")
    if raw:
        m = re.search(r"sec\s*=\s*(\d+)", raw)
        if m:
            return time.time() - int(m.group(1))
    return None


def machine_stats(procs: dict[int, dict]) -> dict:
    total_mem, used_mem = memory()
    ncpu = os.cpu_count() or 1
    cpu_sum = sum(r["cpu_pct"] for r in procs.values())
    try:
        load = os.getloadavg()
    except (OSError, AttributeError):
        load = (0.0, 0.0, 0.0)
    try:
        disk = shutil.disk_usage("/")
        disk_total, disk_used = disk.total, disk.used
    except OSError:
        disk_total = disk_used = 0
    return {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "cpu_count": ncpu,
        "cpu_pct": round(min(100.0, cpu_sum / ncpu), 1),
        "cpu_pct_raw": round(cpu_sum, 1),
        "load1": round(load[0], 2),
        "load5": round(load[1], 2),
        "load15": round(load[2], 2),
        "mem_total": total_mem,
        "mem_used": used_mem,
        "mem_pct": round(used_mem / total_mem * 100, 1) if total_mem else 0.0,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "uptime_s": uptime_seconds(),
        "proc_count": len(procs),
    }


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------


def build(interval: float) -> dict:
    started = time.monotonic()
    binary = find_herdr()

    snapshot: dict = {}
    procinfo: dict[str, dict] = {}
    herdr_meta = {"found": bool(binary), "binary": binary}
    if binary:
        try:
            snapshot, procinfo = collect_herdr(binary)
            herdr_meta.update(
                {
                    "version": snapshot.get("version"),
                    "protocol": snapshot.get("protocol"),
                    "focused_pane_id": snapshot.get("focused_pane_id"),
                    "running": True,
                }
            )
        except Exception as exc:  # noqa: BLE001 - report, don't crash the poll
            herdr_meta["running"] = False
            herdr_meta["error"] = str(exc)
            warn(f"herdr not reachable: {exc}")
    else:
        herdr_meta["running"] = False
        herdr_meta["error"] = "herdr binary not found"

    procs, _elapsed = sample_pair(interval)
    kids = children_map(procs)

    agents_by_pane = {a["pane_id"]: a for a in snapshot.get("agents", [])}
    claimed: set[int] = set()

    # git state only for panes running an agent — that's what the dashboard shows
    repos = collect_git([a.get("cwd") for a in snapshot.get("agents", [])])

    # resolve every pane's process tree first, so sockets can be looked up in one pass
    trees: dict[str, list[int]] = {}
    for pane in snapshot.get("panes", []):
        info = procinfo.get(pane["pane_id"]) or {}
        shell_pid = info.get("shell_pid")
        trees[pane["pane_id"]] = (
            descendants(shell_pid, kids) if shell_pid and shell_pid in procs else []
        )
    sockets = listening_ports({pid for tree in trees.values() for pid in tree})

    panes_out = []
    for pane in snapshot.get("panes", []):
        pane_id = pane["pane_id"]
        info = procinfo.get(pane_id) or {}
        shell_pid = info.get("shell_pid")
        tree = trees[pane_id]
        claimed.update(tree)
        agent = agents_by_pane.get(pane_id)
        fg = [
            {"pid": p.get("pid"), "name": p.get("name"), "cmdline": (p.get("cmdline") or "")[:160]}
            for p in info.get("foreground_processes", [])
        ]
        panes_out.append(
            {
                "pane_id": pane_id,
                "tab_id": pane.get("tab_id"),
                "workspace_id": pane.get("workspace_id"),
                "label": pane.get("label"),
                "cwd": pane.get("cwd"),
                "foreground_cwd": pane.get("foreground_cwd"),
                "focused": pane.get("focused", False),
                "agent": pane.get("agent"),
                "agent_status": pane.get("agent_status", "unknown"),
                "title": pane.get("terminal_title_stripped") or pane.get("terminal_title"),
                "is_agent": agent is not None,
                "state_change_seq": (agent or {}).get("state_change_seq"),
                "agent_session": pane.get("agent_session"),
                "state_labels": pane.get("state_labels"),
                "tokens": pane.get("tokens"),
                "shell_pid": shell_pid,
                "tty": info.get("tty"),
                "foreground": fg,
                "git": repos.get(pane.get("cwd")) if agent else None,
                "ports": ports_for(tree, sockets, procs),
                "usage": usage_for(tree, procs),
            }
        )

    # everything the panes don't account for, minus kernel/pid-0 noise
    other = [pid for pid in procs if pid not in claimed]

    return {
        "schema": SCHEMA,
        "collected_at": time.time(),
        "sample_interval": interval,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "machine": machine_stats(procs),
        "herdr": herdr_meta,
        "panes": panes_out,
        "workspaces": snapshot.get("workspaces", []),
        "tabs": snapshot.get("tabs", []),
        "unattributed": usage_for(other, procs, top=6),
        "warnings": warnings,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--interval",
        type=float,
        default=0.7,
        help="seconds between the two CPU samples (default: 0.7)",
    )
    ap.add_argument("--pretty", action="store_true", help="indent the JSON output")
    args = ap.parse_args()

    try:
        data = build(max(0.05, args.interval))
    except Exception as exc:  # noqa: BLE001 - always emit parseable JSON
        json.dump({"schema": SCHEMA, "fatal": str(exc), "collected_at": time.time()}, sys.stdout)
        sys.stdout.write("\n")
        return 1

    json.dump(data, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
