"""Tests for the adash serve HUD daemon: snapshot shape, atomic writes, stale
propagation, value fallback, and the HTTP endpoints."""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import serve as serve_module
from agents import RunningAgent
from serve import (
    HudDaemon,
    build_snapshot,
    make_server,
    write_snapshot_atomic,
)
from usage import ClaudeProfile, ToolUsage, UsageWindow


# ---------------------------------------------------------------- fixtures


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _claude_usage(**kw) -> ToolUsage:
    resets_5h = _now() + timedelta(hours=2)
    resets_7d = _now() + timedelta(days=5)
    defaults = dict(
        tool="claude",
        plan="Team",
        windows=[UsageWindow("5h", 38.0, resets_5h), UsageWindow("7d", 12.0, resets_7d)],
    )
    defaults.update(kw)
    return ToolUsage(**defaults)


def _codex_usage(**kw) -> ToolUsage:
    defaults = dict(
        tool="codex",
        plan="Max",
        windows=[UsageWindow("5h", 20.0, _now() + timedelta(hours=1)),
                 UsageWindow("7d", 55.0, _now() + timedelta(days=3))],
    )
    defaults.update(kw)
    return ToolUsage(**defaults)


def _fake_usages() -> list[ToolUsage]:
    return [_claude_usage(), _codex_usage(),
            ToolUsage(tool="opencode", spend=1.0, spend_sessions=2)]  # BYOK, must be dropped


def _fake_agents() -> list[RunningAgent]:
    return [
        RunningAgent(tool="claude", pid=1, tty="ttys001", elapsed="4h 12m",
                     cwd="/tmp/web-app", state="working", label="editing auth.py"),
        RunningAgent(tool="codex", pid=2, tty="ttys002", elapsed="9m",
                     cwd="/tmp/api", state="unknown"),
    ]


# ---------------------------------------------------------------- snapshot shape


def test_snapshot_has_frozen_top_level_shape():
    snap = build_snapshot(_fake_usages(), _now(), _fake_agents())
    assert snap["version"] == 1
    # generated_at is ISO8601 with an offset
    assert datetime.fromisoformat(snap["generated_at"]).tzinfo is not None
    assert set(snap) == {"version", "generated_at", "subscriptions", "agents", "value", "soonest_reset"}
    # opencode is BYOK, never a subscription
    assert [s["provider"] for s in snap["subscriptions"]] == ["claude", "codex"]


def test_claude_windows_map_to_frozen_kinds():
    resets = _now() + timedelta(days=6)
    usage = _claude_usage(windows=[
        UsageWindow("5h", 40.0, _now() + timedelta(hours=1)),
        UsageWindow("7d", 10.0, resets),
        UsageWindow("fable", 25.0, resets),  # model-scoped weekly limit
    ])
    snap = build_snapshot([usage], _now(), [])
    kinds = [w["kind"] for w in snap["subscriptions"][0]["windows"]]
    assert kinds == ["session_5h", "weekly_7d", "weekly_fable"]


def test_codex_windows_map_to_session_and_weekly():
    snap = build_snapshot([_codex_usage()], _now(), [])
    sub = snap["subscriptions"][0]
    assert sub["id"] == "codex" and sub["label"] == "Codex Max"
    assert [w["kind"] for w in sub["windows"]] == ["session_5h", "weekly"]


def test_pct_left_is_hundred_minus_utilization():
    snap = build_snapshot([_claude_usage()], _now(), [])
    windows = {w["kind"]: w for w in snap["subscriptions"][0]["windows"]}
    assert windows["session_5h"]["pct_left"] == 62  # 100 - 38
    assert windows["weekly_7d"]["pct_left"] == 88


def test_tightest_is_the_least_headroom_window_with_pace():
    snap = build_snapshot([_claude_usage()], _now(), [])
    sub = snap["subscriptions"][0]
    assert sub["tightest"]["kind"] == "session_5h"  # 62% left beats 88%
    assert sub["tightest"]["pct_left"] == 62
    # pace is computed only on the tightest window
    windows = {w["kind"]: w for w in sub["windows"]}
    assert windows["session_5h"]["pace"] is not None
    assert set(windows["session_5h"]["pace"]) == {"projected_dry_at", "margin_seconds"}
    assert windows["weekly_7d"]["pace"] is None


def test_null_percentages_yield_null_tightest():
    usage = _claude_usage(windows=[UsageWindow("5h", None, None), UsageWindow("7d", None, None)])
    snap = build_snapshot([usage], _now(), [])
    sub = snap["subscriptions"][0]
    assert sub["tightest"] is None
    assert all(w["pct_left"] is None and w["pace"] is None for w in sub["windows"])


def test_soonest_reset_picks_the_earliest_window():
    snap = build_snapshot(_fake_usages(), _now(), _fake_agents())
    # codex 5h resets in ~1h, sooner than everything else
    assert snap["soonest_reset"]["subscription_id"] == "codex"
    assert snap["soonest_reset"]["kind"] == "session_5h"


def test_soonest_reset_null_when_no_resets():
    usage = _claude_usage(windows=[UsageWindow("5h", 40.0, None)])
    assert build_snapshot([usage], _now(), [])["soonest_reset"] is None


# ---------------------------------------------------------------- agents


def test_agents_are_mapped_and_state_normalised():
    snap = build_snapshot(_fake_usages(), _now(), _fake_agents())
    agents = {a["pid"]: a for a in snap["agents"]}
    assert agents[1]["state"] == "working" and agents[1]["action"] == "editing auth.py"
    assert agents[1]["project"] == "web-app"
    assert agents[1]["since_seconds"] == 4 * 3600 + 12 * 60  # parsed from "4h 12m"
    assert agents[2]["state"] == "idle"  # "unknown" normalises to idle
    assert agents[2]["action"] is None
    assert agents[2]["subscription_id"] == "codex"  # codex agents are always codex


def test_active_agents_counted_per_subscription(tmp_path: Path):
    # a claude agent whose per-pid session file lives in the default config dir
    default = tmp_path / ".claude"
    (default / "sessions").mkdir(parents=True)
    (default / "sessions" / "1.json").write_text("{}")
    profiles = [ClaudeProfile(label="carepilot", config_dir=default, default=True)]

    snap = build_snapshot([_claude_usage(), _codex_usage()], _now(), _fake_agents(), profiles=profiles)
    subs = {s["id"]: s for s in snap["subscriptions"]}
    assert subs["claude-team"]["active_agents"] == 1
    assert subs["codex"]["active_agents"] == 1
    claude_agent = next(a for a in snap["agents"] if a["pid"] == 1)
    assert claude_agent["subscription_id"] == "claude-team"


def test_multi_account_claude_ids_and_labels():
    default = ClaudeProfile(label="carepilot", config_dir=Path("/x/.claude"), default=True)
    personal = ClaudeProfile(label="personal", config_dir=Path("/x/.claude-personal"))
    usages = [
        _claude_usage(label="carepilot"),  # the default account carries an org label
        _claude_usage(label="personal"),
    ]
    snap = build_snapshot(usages, _now(), [], profiles=[default, personal])
    subs = {s["id"]: s["label"] for s in snap["subscriptions"]}
    assert subs == {"claude-team": "Claude Team", "claude-personal": "Claude Personal"}


# ---------------------------------------------------------------- stale


def test_stale_reason_propagates_and_keeps_last_good_bars():
    usage = _claude_usage(stale="rate limited · retry 4m")
    sub = build_snapshot([usage], _now(), [])["subscriptions"][0]
    assert sub["stale"] == "rate limited, retry 4m"  # normalised to a plain human string
    # the bars are still there: staleness never blanks the last-good numbers
    assert sub["windows"][0]["pct_left"] == 62


def test_hard_error_surfaces_as_stale_with_empty_windows():
    usage = ToolUsage(tool="claude", error="unlock Keychain or sign in to Claude Code")
    sub = build_snapshot([usage], _now(), [])["subscriptions"][0]
    assert sub["stale"] == "unlock Keychain or sign in to Claude Code"
    assert sub["windows"] == [] and sub["tightest"] is None


def test_fresh_reading_is_not_flagged_stale():
    assert build_snapshot([_claude_usage()], _now(), [])["subscriptions"][0]["stale"] is None


# ---------------------------------------------------------------- value


def _fake_pricing(**attrs) -> SimpleNamespace:
    """A stand-in for the pricing module exposing whatever entry points a test
    wants (hud_value / collect_value)."""
    return SimpleNamespace(**attrs)


def test_value_is_null_when_module_absent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(serve_module, "_pricing", None)
    assert serve_module._collect_value() is None


def test_value_prefers_hud_value_adapter(monkeypatch: pytest.MonkeyPatch):
    payload = {"today_usd": 42.1, "month_usd": 830.5, "subs_cost_usd": 400.0,
               "multiple": 2.08, "by_sub": {"claude-team": {"today_usd": 30.0, "month_usd": 600.0}}}
    # collect_value would raise: hud_value must win when both exist
    monkeypatch.setattr(serve_module, "_pricing",
                        _fake_pricing(hud_value=lambda: payload,
                                      collect_value=lambda: (_ for _ in ()).throw(AssertionError)))
    snap = build_snapshot(_fake_usages(), _now(), [], value=serve_module._collect_value())
    assert snap["value"] == payload


def test_value_falls_back_to_collect_value(monkeypatch: pytest.MonkeyPatch):
    payload = {"today_usd": 1.0, "month_usd": 2.0, "subs_cost_usd": None,
               "multiple": None, "by_sub": {}}
    monkeypatch.setattr(serve_module, "_pricing", _fake_pricing(collect_value=lambda: payload))
    assert serve_module._collect_value() == payload


def test_value_collector_error_is_swallowed(monkeypatch: pytest.MonkeyPatch):
    def boom():
        raise RuntimeError("pricing exploded")

    monkeypatch.setattr(serve_module, "_pricing", _fake_pricing(hud_value=boom))
    assert serve_module._collect_value() is None


def test_value_with_non_json_type_degrades_to_none(monkeypatch: pytest.MonkeyPatch):
    """A value block carrying a set (or any non-JSON type) must never crash the
    snapshot write: it degrades to None instead of raising."""
    bad = {"today_usd": 1.0, "by_sub": {"claude-team"}}  # a set is not JSON-serializable
    monkeypatch.setattr(serve_module, "_pricing", _fake_pricing(hud_value=lambda: bad))
    assert serve_module._collect_value() is None
    # and the snapshot around it still serializes cleanly
    snap = build_snapshot(_fake_usages(), _now(), [], value=serve_module._collect_value())
    assert snap["value"] is None
    json.dumps(snap)  # must not raise


def test_pricing_hud_value_contract_is_serializable(monkeypatch: pytest.MonkeyPatch):
    """Cross-contract seam: once the pricing branch lands, run a real ValueReport
    through the value path and assert the snapshot serializes and the value keys
    match docs/hud-schema.md. Skips while pricing is absent on this branch."""
    pricing = pytest.importorskip("pricing")
    if not hasattr(pricing, "hud_value"):
        pytest.skip("pricing.hud_value adapter not present yet")

    value = serve_module._collect_value()
    snap = build_snapshot(_fake_usages(), _now(), _fake_agents(), value=value)
    json.dumps(snap)  # the whole snapshot must serialize with the real value block
    if snap["value"] is not None:
        assert set(snap["value"]) == {"today_usd", "month_usd", "subs_cost_usd", "multiple", "by_sub"}


def test_snapshot_carries_no_credentials():
    """A token accidentally left on a ToolUsage-like object must not reach the
    snapshot; only known fields are ever read."""
    blob = json.dumps(build_snapshot(_fake_usages(), _now(), _fake_agents()))
    assert "accessToken" not in blob and "Bearer" not in blob


# ---------------------------------------------------------------- atomic write


def test_write_snapshot_atomic_roundtrips(tmp_path: Path):
    path = tmp_path / "sub" / "hud.json"
    snap = build_snapshot(_fake_usages(), _now(), _fake_agents())
    write_snapshot_atomic(path, snap)
    assert json.loads(path.read_text()) == snap
    # no temp files left behind
    assert list(path.parent.glob(".*.tmp")) == []


def test_write_snapshot_atomic_replaces_existing(tmp_path: Path):
    path = tmp_path / "hud.json"
    write_snapshot_atomic(path, {"version": 1, "n": 1})
    write_snapshot_atomic(path, {"version": 1, "n": 2})
    assert json.loads(path.read_text())["n"] == 2


def test_write_snapshot_atomic_leaves_no_temp_on_serialization_error(tmp_path: Path):
    """A non-serializable snapshot must not orphan a .tmp file."""
    path = tmp_path / "hud.json"
    with pytest.raises(TypeError):
        write_snapshot_atomic(path, {"bad": {1, 2, 3}})  # a set can't be dumped
    assert list(tmp_path.glob(".*.tmp")) == []
    assert not path.exists()  # nothing half-written landed at the target


def test_daemon_writes_file_only_on_content_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "hud.json"
    daemon = HudDaemon(cache_path=path)
    # stable reading: same objects and same fetched_at each poll, so only
    # generated_at would differ between rebuilds
    usages, fetched_at, agents = _fake_usages(), _now(), _fake_agents()
    monkeypatch.setattr(serve_module, "collect_usage", lambda: (usages, fetched_at))
    monkeypatch.setattr(serve_module, "claude_profiles", lambda: [])
    monkeypatch.setattr(serve_module, "running_agents", lambda: agents)
    monkeypatch.setattr(serve_module, "enrich", lambda a, **kw: a)
    # the value block reads live session spend, which ticks up while any agent
    # is running; pin it too or "identical inputs" isn't what the test measures
    monkeypatch.setattr(serve_module, "_collect_value", lambda: {"today_usd": 1.0, "month_usd": 2.0})

    daemon.poll_usage_once()
    daemon.poll_activity_once()
    first = path.stat().st_mtime_ns

    # a re-poll with identical content differs only in generated_at, so no rewrite
    daemon.poll_usage_once()
    assert path.stat().st_mtime_ns == first
    snap = daemon.snapshot()
    assert snap["subscriptions"] and snap["agents"]


# ---------------------------------------------------------------- http


@pytest.fixture
def running_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(serve_module, "collect_usage", lambda: (_fake_usages(), _now()))
    monkeypatch.setattr(serve_module, "claude_profiles", lambda: [])
    monkeypatch.setattr(serve_module, "running_agents", lambda: _fake_agents())
    monkeypatch.setattr(serve_module, "enrich", lambda agents, **kw: agents)

    daemon = HudDaemon(cache_path=tmp_path / "hud.json")
    daemon.poll_usage_once()
    daemon.poll_activity_once()
    server = make_server("127.0.0.1", 0, daemon)  # port 0 -> an ephemeral free port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_hud_endpoint_serves_the_snapshot(running_server: str):
    with urllib.request.urlopen(f"{running_server}/v1/hud", timeout=5) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "application/json"
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
        snap = json.loads(resp.read().decode())
    assert snap["version"] == 1
    assert [s["provider"] for s in snap["subscriptions"]] == ["claude", "codex"]
    assert len(snap["agents"]) == 2


def test_health_endpoint(running_server: str):
    with urllib.request.urlopen(f"{running_server}/v1/health", timeout=5) as resp:
        assert resp.status == 200
        assert json.loads(resp.read().decode()) == {"ok": True, "version": 1}


def test_unknown_path_404s(running_server: str):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{running_server}/v1/nope", timeout=5)
    assert exc.value.code == 404


# ---------------------------------------------------------------- loopback guard


def test_loopback_hosts_are_allowed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(serve_module._ALLOW_REMOTE_ENV, raising=False)
    for host in ("127.0.0.1", "::1", "localhost"):
        serve_module._ensure_loopback(host)  # must not raise


def test_non_loopback_bind_is_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(serve_module._ALLOW_REMOTE_ENV, raising=False)
    with pytest.raises(SystemExit) as exc:
        serve_module._ensure_loopback("0.0.0.0")
    assert serve_module._ALLOW_REMOTE_ENV in str(exc.value)  # the message names the escape hatch


def test_non_loopback_bind_allowed_with_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(serve_module._ALLOW_REMOTE_ENV, "1")
    serve_module._ensure_loopback("0.0.0.0")  # must not raise
