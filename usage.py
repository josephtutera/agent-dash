"""Subscription usage collectors.

- Claude: live usage API (api/oauth/usage) using the OAuth token Claude Code
  stores in the macOS Keychain. Same endpoint the /usage command uses.
- Codex: rate_limit snapshots embedded in local rollout files; the newest one
  wins. No API call needed, but data is only as fresh as your last Codex turn.
- OpenCode: no subscription quota (BYOK), so we report 7-day API spend from
  its local database instead.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from collectors import _parse_ts

USAGE_CACHE_TTL_SECONDS = 120


@dataclass
class UsageWindow:
    label: str  # "5h", "7d", ...
    pct: float | None  # 0-100
    resets_at: datetime | None = None


@dataclass
class ToolUsage:
    tool: str
    plan: str = ""
    windows: list[UsageWindow] = field(default_factory=list)
    note: str = ""
    error: str | None = None
    label: str = ""  # claude profile label (e.g. "personal"); "" when single-account
    active: bool = True  # for multi-profile claude: is this the plan new sessions use?
    # BYOK tools (opencode) have no quota %, so they report dollar spend instead;
    # the renderer drops this into the 7d column in place of a utilization bar.
    spend: float | None = None
    spend_sessions: int | None = None
    spend_days: int = 7


# ---------------------------------------------------------------- claude


def _pretty_plan(raw: str) -> str:
    return raw.strip().replace("claude_", "").replace("_", " ").title()


def _read_json(path: Path) -> dict | None:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


@dataclass
class ClaudeProfile:
    label: str  # short name shown in the plan toggle, e.g. "personal"
    config_dir: Path  # the account's ~/.claude tree (default or a CLAUDE_CONFIG_DIR)
    default: bool = False  # the built-in ~/.claude, launched without CLAUDE_CONFIG_DIR


def claude_profiles(home: Path | None = None) -> list[ClaudeProfile]:
    """Discover Claude accounts: the default ~/.claude plus any sibling
    ~/.claude-<name> trees created with CLAUDE_CONFIG_DIR for extra accounts."""
    home = Path(home) if home else Path.home()
    profiles: list[ClaudeProfile] = []
    default = home / ".claude"
    if default.is_dir():
        profiles.append(ClaudeProfile(label=_profile_label(default, "default"), config_dir=default, default=True))
    for d in sorted(home.glob(".claude-*")):
        if d.is_dir():
            # the dir suffix is the user's own name for the account (~/.claude-personal)
            suffix = d.name[len(".claude-"):] or d.name
            profiles.append(ClaudeProfile(label=suffix, config_dir=d))
    return profiles or [ClaudeProfile(label="default", config_dir=default, default=True)]


def _profile_label(config_dir: Path, fallback: str) -> str:
    """A short label from the account's own metadata, else the dir suffix."""
    account = (_read_json(config_dir / ".claude.json") or {}).get("oauthAccount") or {}
    org = account.get("organizationName")
    if org and account.get("organizationType") == "claude_team":
        return org.split()[0].lower()
    email = account.get("emailAddress")
    if email and "@" in email:
        return email.split("@")[0]
    return fallback


def _keychain_service(profile: ClaudeProfile) -> str:
    """The macOS Keychain service Claude Code stores this account's token under.
    The default ~/.claude uses the bare name; a non-default CLAUDE_CONFIG_DIR
    gets a `-<sha256(path)[:8]>` suffix (verified against Claude Code 2.1.x)."""
    if profile.default:
        return "Claude Code-credentials"
    digest = hashlib.sha256(str(profile.config_dir).encode()).hexdigest()[:8]
    return f"Claude Code-credentials-{digest}"


def _profile_credentials(profile: ClaudeProfile) -> tuple[str, str] | None:
    """(access_token, plan) for a profile. Prefer the config dir's own
    .credentials.json (Linux); on macOS fall back to the Keychain entry for
    this account (bare name for the default, hashed suffix otherwise)."""
    creds = _read_json(profile.config_dir / ".credentials.json")
    oauth = (creds or {}).get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if token:
        return token, _pretty_plan(oauth.get("subscriptionType", ""))
    return _keychain_claude_credentials(_keychain_service(profile))


def _keychain_claude_credentials(service: str = "Claude Code-credentials") -> tuple[str, str] | None:
    """Return (access_token, plan) from a Claude Code Keychain entry, or None."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        oauth = json.loads(out.stdout).get("claudeAiOauth", {})
    except ValueError:
        return None
    token = oauth.get("accessToken")
    if not token:
        return None
    return token, _pretty_plan(oauth.get("subscriptionType", ""))


def _parse_claude_usage(payload: dict) -> list[UsageWindow]:
    windows = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        data = payload.get(key)
        if not isinstance(data, dict):
            continue
        pct = data.get("utilization")
        windows.append(
            UsageWindow(
                label=label,
                pct=float(pct) if pct is not None else None,
                resets_at=_parse_ts(data.get("resets_at")),
            )
        )
    return windows


def _claude_usage_from_token(token: str, plan: str) -> ToolUsage:
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.0.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:  # network down, token expired, etc.
        return ToolUsage(tool="claude", plan=plan, error=str(exc)[:60])
    return ToolUsage(tool="claude", plan=plan, windows=_parse_claude_usage(payload))


def fetch_claude_usage_for(profile: ClaudeProfile) -> ToolUsage:
    creds = _profile_credentials(profile)
    if not creds:
        return ToolUsage(tool="claude", error="unlock Keychain or sign in to Claude Code")
    return _claude_usage_from_token(*creds)


def fetch_claude_usages(active_label: str | None = None) -> list[ToolUsage]:
    """One ToolUsage per Claude account. `label` is set (and the active plan
    flagged) only when more than one account exists, so single-account setups
    render exactly as before."""
    profiles = claude_profiles()
    usages = []
    for profile in profiles:
        usage = fetch_claude_usage_for(profile)
        if len(profiles) > 1:
            usage.label = profile.label
        usages.append(usage)
    if len(profiles) > 1:
        active = active_label if active_label in {p.label for p in profiles} else profiles[0].label
        for usage, profile in zip(usages, profiles):
            usage.active = profile.label == active
    return usages


def fetch_claude_usage() -> ToolUsage:
    """Back-compat single-account fetch (first profile)."""
    return fetch_claude_usages()[0]


# ---------------------------------------------------------------- codex


def _window_label(minutes) -> str:
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "7d"
    if isinstance(minutes, (int, float)):
        hours = minutes / 60
        if hours >= 24 and hours % 24 == 0:
            return f"{int(hours / 24)}d"
        if hours >= 1:
            return f"{int(hours)}h"
    return f"{minutes}m"


def fetch_codex_usage(root: Path | None = None, scan: int = 40) -> ToolUsage:
    root = root or Path.home() / ".codex"
    sessions_dir = root / "sessions"
    if not sessions_dir.is_dir():
        return ToolUsage(tool="codex", error="no codex sessions found")
    files = sorted(sessions_dir.glob("**/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:scan]
    for path in files:
        latest = None
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        payload = json.loads(line).get("payload") or {}
                    except ValueError:
                        continue
                    rl = payload.get("rate_limits")
                    if isinstance(rl, dict):
                        latest = rl
        except OSError:
            continue
        if latest is None:
            continue
        windows = []
        for key in ("secondary", "primary"):  # show the short window first
            data = latest.get(key)
            if not isinstance(data, dict):
                continue
            resets = data.get("resets_at")
            windows.append(
                UsageWindow(
                    label=_window_label(data.get("window_minutes")),
                    pct=data.get("used_percent"),
                    resets_at=datetime.fromtimestamp(resets, tz=timezone.utc) if resets else None,
                )
            )
        age = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return ToolUsage(
            tool="codex",
            plan=(latest.get("plan_type") or "").title(),
            windows=windows,
            note=f"as of {age.astimezone().strftime('%b %d %-I:%M %p')}",
        )
    return ToolUsage(tool="codex", error="no rate-limit data yet")


# ---------------------------------------------------------------- opencode


def fetch_opencode_usage(db_path: Path | None = None) -> ToolUsage:
    db_path = db_path or Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if not db_path.is_file():
        return ToolUsage(tool="opencode", error="no opencode database found")
    cutoff_ms = int((time.time() - 7 * 86400) * 1000)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            count, spend = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM session "
                "WHERE time_archived IS NULL AND time_updated > ?",
                (cutoff_ms,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return ToolUsage(tool="opencode", error=str(exc)[:60])
    return ToolUsage(
        tool="opencode",
        plan="pay-as-you-go",
        spend=float(spend),
        spend_sessions=int(count),
        spend_days=7,
        # kept for the --usage text dump and back-compat; the TUI uses the fields
        note=f"${spend:.2f} across {count} session{'s' if count != 1 else ''} · 7d",
    )


# ---------------------------------------------------------------- combined


def collect_usage(active_claude: str | None = None) -> tuple[list[ToolUsage], datetime]:
    usages = [*fetch_claude_usages(active_claude), fetch_codex_usage(), fetch_opencode_usage()]
    return usages, datetime.now(timezone.utc)
