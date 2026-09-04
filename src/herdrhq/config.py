"""YAML configuration, with automatic migration from the legacy config.json.

Looked up in this order (first existing file wins):

    $HERDRHQ_CONFIG
    ./herdr-hq.yaml
    ~/.config/herdr-hq/herdr-hq.yaml
    ./config.json               (legacy herdr-hq <= 0.1 format)

Example:

    listen:
      host: 127.0.0.1
      port: 8787
      password: s3cret          # optional: permanent secret instead of the
                                # per-run token; auth: none disables auth
    poll:
      interval: 6.0             # seconds between collector runs per host
      sample_interval: 0.7      # cpu sampling window inside the collector
      timeout: 30.0             # give up on a poll after this long
      history: 120              # sparkline depth (samples)

    terminal:
      enabled: true
      input: true               # allow typing into mirrored panes
      interval: 0.25            # pane read cadence
      max_sessions: 6

    events:
      enabled: true             # push bridge via herdr events.subscribe

    hosts:
      local:
        transport: local        # runs the collector on this machine
      nixos: {}                 # ssh host; target defaults to the name
      nixos-ultra:
        http:
          jupyter: 8888         # http://jupyter.nixos-ultra.localhost:8787/
        tunnels:
          - 5432                # 127.0.0.1:5432 -> nixos-ultra localhost:5432

SSH authentication is whatever plain `ssh <target>` does: asyncssh reads
~/.ssh/config, ~/.ssh/known_hosts and talks to the ssh-agent.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("herdrhq.config")


@dataclass
class Tunnel:
    local: int
    remote_host: str
    remote_port: int


@dataclass
class HttpService:
    name: str
    remote_host: str
    remote_port: int


@dataclass
class HostSpec:
    name: str
    transport: str = "ssh"  # "ssh" | "local"
    target: str | None = None  # ssh destination; defaults to name
    python: str | None = None  # interpreter on the host
    tunnels: list[Tunnel] = field(default_factory=list)
    http: list[HttpService] = field(default_factory=list)

    @property
    def ssh_target(self) -> str:
        return self.target or self.name


@dataclass
class Config:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8787
    password: str | None = None  # permanent auth secret (else per-run token)
    auth_enabled: bool = True
    poll_interval: float = 6.0
    sample_interval: float = 0.7
    poll_timeout: float = 30.0
    history: int = 120
    interval_when_live: float = 15.0  # poll cadence while the event bridge is up
    terminal_enabled: bool = True
    terminal_input: bool = True
    terminal_interval: float = 0.06  # max mirror read cadence (herdr updates ~10 Hz)
    terminal_max_sessions: int = 6
    events_enabled: bool = True
    hosts: dict[str, HostSpec] = field(default_factory=dict)
    path: Path | None = None


def _parse_target(v: object, default_host: str = "localhost") -> tuple[str, int]:
    """Accept 8888, "8888" or "somehost:8888"."""
    if isinstance(v, int):
        return default_host, v
    s = str(v)
    if ":" in s:
        h, p = s.rsplit(":", 1)
        return h, int(p)
    return default_host, int(s)


def _parse_host(name: str, spec: dict | None) -> HostSpec:
    spec = spec or {}
    hs = HostSpec(
        name=str(name),
        transport=str(spec.get("transport", "ssh")),
        target=spec.get("target"),
        python=spec.get("python"),
    )
    for t in spec.get("tunnels") or []:
        if isinstance(t, dict):
            rh, rp = _parse_target(t.get("remote"))
            local = int(t.get("local", rp))
        else:
            rh, rp = _parse_target(t)
            local = rp
        hs.tunnels.append(Tunnel(local=local, remote_host=rh, remote_port=rp))
    for sname, target in (spec.get("http") or {}).items():
        rh, rp = _parse_target(target)
        hs.http.append(HttpService(name=str(sname), remote_host=rh, remote_port=rp))
    return hs


def _parse(data: dict, path: Path | None) -> Config:
    cfg = Config(path=path)
    listen = data.get("listen") or {}
    cfg.listen_host = str(listen.get("host", cfg.listen_host))
    cfg.listen_port = int(listen.get("port", cfg.listen_port))
    if listen.get("password"):
        cfg.password = str(listen["password"])
    cfg.auth_enabled = str(listen.get("auth", "on")).lower() not in (
        "none", "off", "false", "no",
    )

    poll = data.get("poll") or {}
    cfg.poll_interval = float(poll.get("interval", cfg.poll_interval))
    cfg.sample_interval = float(poll.get("sample_interval", cfg.sample_interval))
    cfg.poll_timeout = float(poll.get("timeout", cfg.poll_timeout))
    cfg.history = int(poll.get("history", cfg.history))
    cfg.interval_when_live = float(poll.get("interval_when_live", cfg.interval_when_live))

    term = data.get("terminal") or {}
    cfg.terminal_enabled = bool(term.get("enabled", cfg.terminal_enabled))
    cfg.terminal_input = bool(term.get("input", cfg.terminal_input))
    cfg.terminal_interval = float(term.get("interval", cfg.terminal_interval))
    cfg.terminal_max_sessions = int(term.get("max_sessions", cfg.terminal_max_sessions))

    events = data.get("events") or {}
    cfg.events_enabled = bool(events.get("enabled", cfg.events_enabled))

    for name, spec in (data.get("hosts") or {}).items():
        cfg.hosts[str(name)] = _parse_host(name, spec)
    return cfg


# Legacy config.json keys that shaped the OpenSSH command line; asyncssh reads
# ~/.ssh/config itself, so these no longer apply.
_LEGACY_SSH_KEYS = ("ssh", "ssh_args", "control_path", "connect_timeout")


def _parse_legacy(data: dict, path: Path) -> Config:
    """Parse a herdr-hq <= 0.1 config.json into the same Config."""
    cfg = Config(path=path)
    cfg.listen_host = str(data.get("host", cfg.listen_host))
    cfg.listen_port = int(data.get("port", cfg.listen_port))
    cfg.poll_interval = float(data.get("poll_interval", cfg.poll_interval))
    cfg.sample_interval = float(data.get("sample_interval", cfg.sample_interval))
    cfg.poll_timeout = float(data.get("ssh_timeout", cfg.poll_timeout))
    cfg.history = int(data.get("history", cfg.history))
    cfg.terminal_enabled = bool(data.get("terminal_enabled", cfg.terminal_enabled))
    cfg.terminal_input = bool(data.get("terminal_input", cfg.terminal_input))
    cfg.terminal_interval = float(data.get("terminal_interval", cfg.terminal_interval))
    cfg.terminal_max_sessions = int(data.get("terminal_max_sessions", cfg.terminal_max_sessions))

    seen: set[str] = set()
    for i, spec in enumerate(data.get("hosts") or []):
        name = spec.get("name") or spec.get("target") or f"host{i}"
        while name in seen:
            name += "'"
        seen.add(name)
        dropped = [k for k in _LEGACY_SSH_KEYS if k in spec]
        if dropped:
            log.warning(
                "host %s: ignoring legacy ssh option(s) %s — asyncssh reads "
                "~/.ssh/config directly, move host-specific settings there",
                name, ", ".join(dropped),
            )
        cfg.hosts[name] = HostSpec(
            name=name,
            transport=spec.get("transport", "ssh" if spec.get("target") else "local"),
            target=spec.get("target"),
            python=spec.get("python"),
        )
    return cfg


def load_config(explicit: str | None = None) -> Config:
    candidates: list[tuple[Path, bool]] = []  # (path, legacy_json)
    if explicit:
        p = Path(explicit)
        candidates.append((p, p.suffix == ".json"))
    elif os.environ.get("HERDRHQ_CONFIG"):
        p = Path(os.environ["HERDRHQ_CONFIG"])
        candidates.append((p, p.suffix == ".json"))
    candidates += [
        (Path("herdr-hq.yaml"), False),
        (Path.home() / ".config" / "herdr-hq" / "herdr-hq.yaml", False),
        (Path("config.json"), True),
    ]
    for p, legacy in candidates:
        try:
            if not p.exists():
                continue
            if legacy:
                cfg = _parse_legacy(json.loads(p.read_text()), p)
                log.warning(
                    "loaded legacy %s (%d hosts) — consider migrating to herdr-hq.yaml",
                    p, len(cfg.hosts),
                )
            else:
                cfg = _parse(yaml.safe_load(p.read_text()) or {}, p)
                log.info("loaded config from %s (%d hosts)", p, len(cfg.hosts))
            if not cfg.hosts:
                cfg.hosts["localhost"] = HostSpec(name="localhost", transport="local")
            return cfg
        except (OSError, yaml.YAMLError, json.JSONDecodeError, ValueError, TypeError) as e:
            log.warning("could not read %s: %s", p, e)
    cfg = Config()
    cfg.hosts["localhost"] = HostSpec(name="localhost", transport="local")
    return cfg
