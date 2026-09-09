"""The agent usage probe: claude /usage parsing, codex rollouts, config plumbing."""

import base64
import calendar
import json

from herdrhq.config import _parse
from herdrhq.remote import usage

# verbatim `result` text from `claude -p /usage --output-format json` (2.1.220)
USAGE_TEXT = """\
You are currently using your subscription to power your Claude Code usage

Current session: 9% used · resets Sep 9 at 6:09pm (America/Chicago)
Current week (all models): 4% used · resets Sep 16 at 3am (America/Chicago)
Current week (Fable): 4% used · resets Sep 16 at 2:59am (America/Chicago)

What's contributing to your limits usage?
Approximate, based on local sessions on this machine.

Last 24h · 246 requests · 4 sessions
  71% of your usage was at >150k context
"""


def test_parse_windows_full():
    wins = usage.parse_windows(USAGE_TEXT)
    assert [w["label"] for w in wins] == ["session", "week (all models)", "week (Fable)"]
    assert [w["pct"] for w in wins] == [9, 4, 4]
    assert wins[0]["resets"] == "Sep 9 at 6:09pm (America/Chicago)"
    assert wins[1]["resets"] == "Sep 16 at 3am (America/Chicago)"


def test_parse_windows_minimal():
    text = "Current session: 87% used · resets Sep 9 at 6pm (UTC)\n"
    wins = usage.parse_windows(text)
    assert wins == [{"label": "session", "pct": 87, "resets": "Sep 9 at 6pm (UTC)"}]


def test_parse_windows_no_resets():
    # tolerate the reset clause going missing; keep the percentage
    wins = usage.parse_windows("Current session: 12% used\n")
    assert wins == [{"label": "session", "pct": 12, "resets": None}]


def test_parse_windows_garbage():
    assert usage.parse_windows("The usage screen moved, run /status instead") == []


def test_run_json_missing_binary():
    data, err = usage.run_json(["/definitely/not/claude"], timeout=2.0)
    assert data is None
    assert err


# structure observed in codex 0.153 rollout files (values anonymized)
CODEX_RL = {
    "limit_id": "codex",
    "primary": {"used_percent": 6.4, "window_minutes": 10080, "resets_at": 1788797698},
    "secondary": {"used_percent": 51.0, "window_minutes": 300, "resets_at": 1788790000},
    "plan_type": "prolite",
}


ROLLOUT_TS_EPOCH = float(calendar.timegm((2026, 9, 3, 14, 55, 43, 0, 0, 0)))


def _rollout_line(rl, ts="2026-09-03T14:55:43.153Z"):
    return json.dumps(
        {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": rl}}
    )


def _fake_jwt(claims):
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    return f"{b64(b'{}')}.{b64(json.dumps(claims).encode())}."


def test_codex_windows():
    wins = usage.codex_windows(CODEX_RL)
    assert wins == [
        {"label": "week", "pct": 6, "resets_at": 1788797698},
        {"label": "session", "pct": 51, "resets_at": 1788790000},
    ]


def test_codex_rate_line_picks_newest(tmp_path):
    day = tmp_path / "2026" / "09" / "03"
    day.mkdir(parents=True)
    old = {**CODEX_RL, "primary": {**CODEX_RL["primary"], "used_percent": 1}}
    (day / "rollout-a.jsonl").write_text(
        '{"type":"event_msg","payload":{"type":"other"}}\n'
        + _rollout_line(old, "2026-09-03T09:00:00.000Z") + "\n"
        + _rollout_line(CODEX_RL) + "\n"
    )
    rl, ts, mtime = usage.codex_rate_line(str(tmp_path))
    assert rl == CODEX_RL  # last snapshot in the file wins
    assert usage._iso_epoch(ts) == ROLLOUT_TS_EPOCH
    assert mtime is not None


def test_codex_rate_line_empty(tmp_path):
    assert usage.codex_rate_line(str(tmp_path)) == (None, None, None)


def test_build_codex(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CODEX_DIR", str(tmp_path))
    jwt = _fake_jwt({
        "email": "t@example.com",
        "https://api.openai.com/auth": {"chatgpt_plan_type": "prolite"},
    })
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"id_token": jwt}}))
    day = tmp_path / "sessions" / "2026" / "09" / "03"
    day.mkdir(parents=True)
    (day / "rollout-a.jsonl").write_text(_rollout_line(CODEX_RL) + "\n")
    out = usage.build_codex()
    assert out["account"] == {"email": "t@example.com", "plan": "prolite"}
    assert [w["label"] for w in out["windows"]] == ["week", "session"]
    assert out["as_of"] == ROLLOUT_TS_EPOCH
    assert "error" not in out
    # secrets never leak into the payload
    assert "id_token" not in json.dumps(out)


def test_build_codex_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CODEX_DIR", str(tmp_path / "nope"))
    assert usage.build_codex() is None


def test_build_codex_no_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CODEX_DIR", str(tmp_path))
    (tmp_path / "auth.json").write_text("{}")
    out = usage.build_codex()
    assert out["windows"] == []
    assert out["error"]
    assert out["account"] is None


def test_config_usage_defaults():
    cfg = _parse({}, None)
    assert cfg.usage_enabled is True
    assert cfg.usage_interval == 900.0
    assert cfg.usage_timeout == 30.0


def test_config_usage_section():
    cfg = _parse({"usage": {"enabled": False, "interval": 60, "timeout": 10}}, None)
    assert cfg.usage_enabled is False
    assert cfg.usage_interval == 60.0
    assert cfg.usage_timeout == 10.0
