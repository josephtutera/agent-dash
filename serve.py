"""adash serve: the resident HUD daemon.

One background process that polls subscription usage and live agent activity,
folds them into a single frozen-schema snapshot, writes that snapshot atomically
to ~/.cache/adash/hud.json on every meaningful change, and serves it over a tiny
loopback HTTP API for the Swift HUD app (and any other localhost tooling).

Why a single daemon: the Anthropic usage endpoint is rate-limited per account and
shared with every live Claude Code session, so nothing here reaches around
usage.py's cache and 429 backoff. Usage is polled slowly (180s) and always
through usage.py; live activity (agents, their state) is cheap file reads and one
ps, so it polls fast (2s). All polling is thread-based to match the rest of adash.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import is_dataclass, asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from activity import enrich
from agents import running_agents
from usage import claude_profiles, collect_usage

# Live activity is cheap (file reads + one ps); usage hits a rate-limited API and
# barely moves between reads, so it stays slow and leans on usage.py's own cache.
ACTIVITY_POLL_SECONDS = 2.0
USAGE_POLL_SECONDS = 180.0
SCHEMA_VERSION = 1

# Window durations, used to project a burn line for the tightest window.
_WINDOW_SECONDS = {
    "session_5h": 5 * 3600,
    "weekly_7d": 7 * 86400,
    "weekly_fable": 7 * 86400,
    "weekly": 7 * 86400,
}

# The soft dependency: a sibling branch builds pricing. It may not exist here, so
# import the whole module defensively and treat its absence as a null "value"
# block. The preferred entry point is pricing.hud_value(), which returns exactly
# the documented value contract; older builds only expose collect_value(), which
# hands back a richer object we coerce down.
try:
    import pricing as _pricing  # type: ignore
except ImportError:
    _pricing = None

# Non-loopback binds break the "localhost only" promise (no auth, permissive
# CORS), so they're refused unless this escape hatch is explicitly set.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_ALLOW_REMOTE_ENV = "ADASH_SERVE_ALLOW_REMOTE"


# ---------------------------------------------------------------- helpers


def _default_cache_path() -> Path:
    return Path.home() / ".cache" / "adash" / "hud.json"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _claude_id(profile) -> str:
    """Stable subscription id for a Claude profile. The built-in ~/.claude is
    the team account ("claude-team"); a sibling ~/.claude-<name> keeps its
    suffix ("claude-personal", "claude-work")."""
    if getattr(profile, "default", False):
        return "claude-team"
    name = profile.config_dir.name
    suffix = name[len(".claude-"):] if name.startswith(".claude-") else profile.label
    return f"claude-{suffix or profile.label}"


def _sub_id_for_usage(usage, profiles) -> str:
    """The subscription id a ToolUsage belongs to. Single-account Claude has no
    label and is always the default dir; multi-account matches the profile whose
    label the usage carries so the default's org-derived label still maps to
    'claude-team'."""
    if usage.tool == "codex":
        return "codex"
    if not usage.label:
        return "claude-team"
    for profile in profiles:
        if profile.label == usage.label:
            return _claude_id(profile)
    return f"claude-{usage.label}"


def _sub_label(sub_id: str, usage) -> str:
    if usage.tool == "codex":
        return f"Codex {usage.plan}".strip() if usage.plan else "Codex"
    if sub_id == "claude-team":
        return "Claude Team"
    name = sub_id[len("claude-"):]
    return "Claude " + name.replace("-", " ").title()


def _window_kind(provider: str, label: str) -> str:
    """Map usage.py's window labels onto the frozen schema's window kinds."""
    if provider == "codex":
        return "session_5h" if label == "5h" else "weekly"
    if label == "5h":
        return "session_5h"
    if label == "7d":
        return "weekly_7d"
    return "weekly_fable"  # the model-scoped weekly limit (Fable on Max/Team)


def _pct_left(pct: float | None) -> int | None:
    if pct is None:
        return None
    return max(0, min(100, round(100 - pct)))


def _stale_text(usage) -> str | None:
    """A human reason the bars are old, or None when they're fresh. usage.py
    marks rate-limited/cached reads; a hard error (no reading at all) stands in
    as the reason so a subscription never silently looks fine."""
    raw = usage.stale or usage.error
    if not raw:
        return None
    return raw.replace(" · ", ", ")  # "rate limited · retry 4m" -> "rate limited, retry 4m"


def _pace(used_pct: float | None, resets_at: datetime | None, kind: str, now: datetime) -> dict | None:
    """Linear burn projection for one window: extend the average burn since the
    window opened to when the quota would hit zero, and how many seconds of slack
    that leaves before the window resets. None whenever it can't be computed."""
    if used_pct is None or resets_at is None or used_pct <= 0:
        return None
    duration = _WINDOW_SECONDS.get(kind)
    if not duration:
        return None
    start = resets_at - timedelta(seconds=duration)
    elapsed = (now - start).total_seconds()
    if elapsed <= 0:
        return None
    burn_per_second = used_pct / elapsed
    remaining = 100.0 - used_pct
    dry = now if remaining <= 0 else now + timedelta(seconds=remaining / burn_per_second)
    margin = (resets_at - dry).total_seconds()
    return {"projected_dry_at": dry.isoformat(), "margin_seconds": int(margin)}


def _norm_state(state: str) -> str:
    return state if state in ("working", "waiting", "idle") else "idle"


def _elapsed_seconds(text: str) -> int | None:
    """Turn agents.py's friendly uptime ("2d 4h", "4h 12m", "12m") back into
    seconds. Approximate (the friendly form drops the smaller unit), but enough
    for a HUD's "since" line."""
    total = 0
    matched = False
    for num, unit in re.findall(r"(\d+)\s*([dhm])", text or ""):
        matched = True
        total += int(num) * {"d": 86400, "h": 3600, "m": 60}[unit]
    return total if matched else None


def _agent_sub_id(agent, profiles) -> str | None:
    """The subscription an agent is spending against, when determinable. Codex
    agents are always the codex subscription; a Claude agent is matched to the
    account whose config dir holds its per-pid session file; everything else
    (opencode, unresolved) is null."""
    if agent.tool == "codex":
        return "codex"
    if agent.tool != "claude":
        return None
    for profile in profiles:
        if (profile.config_dir / "sessions" / f"{agent.pid}.json").exists():
            return _claude_id(profile)
    return None


def _coerce_value(result) -> dict | None:
    """Fallback coercion for pricing.collect_value(): a schema-shaped dict passes
    through, a dataclass is unwrapped, anything else (or None) becomes null. This
    is best-effort only; the raw collect_value object can carry non-JSON types
    (sets, datetimes), which is exactly why _validate_json guards the result."""
    if result is None:
        return None
    if isinstance(result, dict):
        return result
    if is_dataclass(result):
        return asdict(result)
    return None


def _validate_json(value) -> dict | None:
    """Guarantee the value block can be serialized before it reaches the snapshot.
    A pricing object that isn't pure JSON (a set, a datetime) would otherwise
    crash json.dump when the snapshot is written, taking the daemon down. Degrade
    to null with a warning instead: the value field is never worth a crash."""
    if value is None:
        return None
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        print(f"adash serve: value block is not JSON-serializable ({exc}); dropping it",
              file=sys.stderr)
        return None
    return value


def _collect_value() -> dict | None:
    """The value block, or None. Prefer pricing.hud_value() (the documented
    contract); fall back to coercing collect_value() on older builds. Any failure,
    including a non-serializable result, degrades to None rather than propagating."""
    if _pricing is None:
        return None
    try:
        if hasattr(_pricing, "hud_value"):
            value = _pricing.hud_value()
        elif hasattr(_pricing, "collect_value"):
            value = _coerce_value(_pricing.collect_value())
        else:
            return None
    except Exception:
        return None
    return _validate_json(value)


# ---------------------------------------------------------------- snapshot


def build_snapshot(usages, fetched_at, agents, value=None, profiles=None) -> dict:
    """Fold collector outputs into a v1 HUD snapshot dict. Pure given its args:
    pass the Claude `profiles` list to resolve multi-account subscription ids
    (an empty list is fine for single-account setups). No credentials ever land
    in the returned dict.

    The burn projection is anchored to `fetched_at` (when the usage was read),
    not wall-clock time, so a rebuild triggered by an activity poll reuses the
    same usage reading and produces byte-identical pace rather than churning."""
    generated_at = datetime.now(timezone.utc)
    now = fetched_at or generated_at  # pace/reset math uses the reading's own timestamp
    profiles = profiles or []

    subscriptions = []
    soonest = None  # (resets_at dt, sub_id, kind)
    for usage in usages:
        if usage.tool not in ("claude", "codex"):
            continue  # opencode is BYOK, not a subscription
        sub_id = _sub_id_for_usage(usage, profiles)
        windows_out = []
        tightest_pair = None  # (window dict, source UsageWindow)
        for win in usage.windows:
            kind = _window_kind(usage.tool, win.label)
            pct_left = _pct_left(win.pct)
            entry = {"kind": kind, "pct_left": pct_left, "resets_at": _iso(win.resets_at), "pace": None}
            windows_out.append(entry)
            if pct_left is not None and (tightest_pair is None or pct_left < tightest_pair[0]["pct_left"]):
                tightest_pair = (entry, win)
            if win.resets_at is not None and (soonest is None or win.resets_at < soonest[0]):
                soonest = (win.resets_at, sub_id, kind)

        tightest = None
        if tightest_pair is not None:
            entry, win = tightest_pair
            entry["pace"] = _pace(win.pct, win.resets_at, entry["kind"], now)
            tightest = {"kind": entry["kind"], "pct_left": entry["pct_left"], "resets_at": entry["resets_at"]}

        subscriptions.append({
            "id": sub_id,
            "provider": usage.tool,
            "label": _sub_label(sub_id, usage),
            "windows": windows_out,
            "tightest": tightest,
            "stale": _stale_text(usage),
            "active_agents": 0,  # filled once agents are resolved
        })

    agents_out = []
    for agent in agents:
        sub_id = _agent_sub_id(agent, profiles)
        agents_out.append({
            "pid": agent.pid,
            "tool": agent.tool,
            "project": Path(agent.cwd).name if agent.cwd else "",
            "cwd": agent.cwd,
            "state": _norm_state(agent.state),
            "action": agent.label or None,
            "since_seconds": _elapsed_seconds(agent.elapsed),
            "subscription_id": sub_id,
        })

    counts: dict[str, int] = {}
    for a in agents_out:
        if a["subscription_id"]:
            counts[a["subscription_id"]] = counts.get(a["subscription_id"], 0) + 1
    for sub in subscriptions:
        sub["active_agents"] = counts.get(sub["id"], 0)

    soonest_reset = None
    if soonest is not None:
        soonest_reset = {"subscription_id": soonest[1], "kind": soonest[2], "resets_at": _iso(soonest[0])}

    return {
        "version": SCHEMA_VERSION,
        "generated_at": _iso(generated_at),
        "subscriptions": subscriptions,
        "agents": agents_out,
        "value": value,
        "soonest_reset": soonest_reset,
    }


def write_snapshot_atomic(path: Path, snapshot: dict) -> None:
    """Write the snapshot without ever exposing a half-written file: dump to a
    temp file in the same directory, then os.replace onto the target. The temp
    file is always cleaned up, so a serialization error can't orphan a .tmp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(snapshot, fh, indent=2)
        os.replace(tmp, path)  # atomic rename; on success tmp no longer exists
    finally:
        tmp.unlink(missing_ok=True)


def _comparable(snapshot: dict) -> dict:
    """The snapshot minus its timestamp, so a rebuild that changed nothing but
    generated_at doesn't churn the file on disk every couple of seconds."""
    return {k: v for k, v in snapshot.items() if k != "generated_at"}


# ---------------------------------------------------------------- daemon


class HudDaemon:
    """Holds the latest collector outputs, rebuilds the snapshot on each poll,
    and serves it. Thread-safe: pollers write components under a lock, readers
    (the HTTP handler) get the current snapshot under the same lock."""

    def __init__(self, cache_path: Path | None = None):
        self.cache_path = cache_path or _default_cache_path()
        self._lock = threading.Lock()
        self._usages: list = []
        self._fetched_at: datetime | None = None
        self._profiles: list = []
        self._value: dict | None = None
        self._agents: list = []
        self._snapshot = build_snapshot([], None, [], None, [])
        self._last_comparable = _comparable(self._snapshot)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot

    def _rebuild(self) -> None:
        with self._lock:
            snapshot = build_snapshot(
                self._usages, self._fetched_at, self._agents, self._value, self._profiles
            )
            self._snapshot = snapshot
            comparable = _comparable(snapshot)
            changed = comparable != self._last_comparable
            self._last_comparable = comparable
        if changed:
            write_snapshot_atomic(self.cache_path, snapshot)

    def poll_usage_once(self) -> None:
        usages, fetched_at = collect_usage()  # goes through usage.py's cache + 429 backoff
        profiles = claude_profiles()
        value = _collect_value()
        with self._lock:
            self._usages, self._fetched_at = usages, fetched_at
            self._profiles, self._value = profiles, value
        self._rebuild()

    def poll_activity_once(self) -> None:
        agents = enrich(running_agents())
        with self._lock:
            self._agents = agents
        self._rebuild()

    def _loop(self, fn, interval: float) -> None:
        while not self._stop.is_set():
            try:
                fn()
            except Exception:  # a bad poll must never kill the loop
                pass
            if self._stop.wait(interval):
                break

    def start_polling(self) -> None:
        for fn, interval in ((self.poll_usage_once, USAGE_POLL_SECONDS),
                             (self.poll_activity_once, ACTIVITY_POLL_SECONDS)):
            thread = threading.Thread(target=self._loop, args=(fn, interval), daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------- http


class _HudHandler(BaseHTTPRequestHandler):
    """GET /v1/hud -> the snapshot; GET /v1/health -> liveness; else 404.
    CORS is permissive because the bind is loopback-only, so 'anyone' is already
    just localhost tooling."""

    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 (http.server naming)
        self._send(204, b"")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/hud":
            body = json.dumps(self.server.hud_daemon.snapshot()).encode()  # type: ignore[attr-defined]
            self._send(200, body)
        elif self.path == "/v1/health":
            self._send(200, json.dumps({"ok": True, "version": SCHEMA_VERSION}).encode())
        else:
            self._send(404, json.dumps({"error": "not found"}).encode())

    def log_message(self, *args) -> None:  # keep the daemon quiet
        pass


def make_server(host: str, port: int, daemon: HudDaemon) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _HudHandler)
    server.hud_daemon = daemon  # type: ignore[attr-defined]
    return server


def _ensure_loopback(host: str) -> None:
    """Refuse a non-loopback bind unless explicitly overridden. The HUD API has no
    auth and permissive CORS, so exposing it off localhost would hand every
    subscription reading and running-agent list to the local network."""
    if host in _LOOPBACK_HOSTS or os.environ.get(_ALLOW_REMOTE_ENV) == "1":
        return
    raise SystemExit(
        f"adash serve: refusing to bind non-loopback host {host!r}. The HUD API has "
        f"no authentication and permissive CORS, so it must stay on localhost. Set "
        f"{_ALLOW_REMOTE_ENV}=1 to override at your own risk."
    )


def serve(host: str = "127.0.0.1", port: int = 8737) -> None:
    """Run the HUD daemon: start the pollers, prime the snapshot once, and serve
    it on the loopback interface until interrupted."""
    _ensure_loopback(host)
    daemon = HudDaemon()
    # prime once so /v1/hud has data on the first request; a failing collector must
    # log and let startup continue rather than kill the daemon (the loop retries).
    for prime in (daemon.poll_usage_once, daemon.poll_activity_once):
        try:
            prime()
        except Exception as exc:
            print(f"adash serve: initial poll failed ({exc}); will retry in the loop",
                  file=sys.stderr)
    daemon.start_polling()
    server = make_server(host, port, daemon)
    print(f"adash serve: HUD snapshot on http://{host}:{port}/v1/hud (cache {daemon.cache_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
        server.shutdown()
        server.server_close()
