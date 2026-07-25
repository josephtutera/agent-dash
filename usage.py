"""Subscription usage collectors.

- Claude: live usage API (api/oauth/usage) using the OAuth token Claude Code
  stores in the macOS Keychain. Same endpoint the /usage command uses. Those
  access tokens only last ~8 hours and Claude Code renews one lazily, when it
  makes a call of its own, so an account you haven't touched today has a dead
  token on disk. We renew it here from the stored refresh token and write the
  result back, which keeps both tools on one credential.
- Codex: rate_limit snapshots embedded in local rollout files; the newest one
  wins. No API call needed, but data is only as fresh as your last Codex turn.
- OpenCode: no subscription quota (BYOK), so we report 7-day API spend from
  its local database instead.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from collectors import _parse_ts

# The usage endpoint rate-limits per account, and every Claude Code session
# polls it too, so the dashboard has to be a light touch: reuse a recent
# reading instead of re-asking, and back off hard once we're told to.
USAGE_CACHE_TTL_SECONDS = 120
RATE_LIMIT_BACKOFF_START = 120.0  # first cooldown after a 429
RATE_LIMIT_BACKOFF_MAX = 900.0  # doubles per repeat 429, capped at 15 minutes
# A credential that needs a human can't heal on the next poll, and re-firing a
# dead token every few minutes is exactly what earns the account a 429. Sit out.
AUTH_FAILURE_COOLDOWN_SECONDS = 900.0

# Claude Code's own OAuth client and token endpoint, read out of the 2.1.x
# binary's string table rather than from memory. Access tokens live ~8 hours and
# are only renewed when Claude Code itself makes a call, so an account you
# haven't used today has a dead token sitting in the Keychain -- which is why
# the dashboard has to be able to refresh one on its own.
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_USER_AGENT = "claude-code/2.0.0"
# Refresh a little before the stated expiry: a token that dies in the next few
# minutes is already useless to a poller that only wakes every three.
TOKEN_REFRESH_SKEW_SECONDS = 300.0


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
    # set when the bars come from cache rather than a live call (e.g. we're in a
    # rate-limit cooldown); the renderer shows it next to the bars.
    stale: str = ""


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


@dataclass
class ClaudeCredentials:
    """One account's stored OAuth blob, plus where it came from so a refreshed
    token can be written back to the same place Claude Code will read it."""

    oauth: dict  # the whole claudeAiOauth object, kept intact for write-back
    keychain_service: str | None = None
    file_path: Path | None = None

    @property
    def token(self) -> str:
        return self.oauth.get("accessToken") or ""

    @property
    def plan(self) -> str:
        return _pretty_plan(self.oauth.get("subscriptionType", "") or "")

    @property
    def refresh_token(self) -> str | None:
        return self.oauth.get("refreshToken")

    @property
    def expires_at(self) -> float | None:
        """Epoch seconds, or None when the entry doesn't record one."""
        raw = self.oauth.get("expiresAt")
        return raw / 1000.0 if isinstance(raw, (int, float)) else None

    def expired(self, skew: float = TOKEN_REFRESH_SKEW_SECONDS) -> bool:
        """True when the access token is past (or nearly past) its life. An
        entry with no expiry recorded can't be judged, so we assume it works
        and let a 401 tell us otherwise."""
        expires = self.expires_at
        return expires is not None and expires - skew <= time.time()


def _profile_credentials(profile: ClaudeProfile) -> ClaudeCredentials | None:
    """The stored credentials for a profile. Prefer the config dir's own
    .credentials.json (Linux); on macOS fall back to the Keychain entry for
    this account (bare name for the default, hashed suffix otherwise)."""
    path = profile.config_dir / ".credentials.json"
    oauth = (_read_json(path) or {}).get("claudeAiOauth") or {}
    if oauth.get("accessToken"):
        return ClaudeCredentials(oauth=oauth, file_path=path)
    return _keychain_claude_credentials(_keychain_service(profile))


def _keychain_claude_credentials(service: str = "Claude Code-credentials") -> ClaudeCredentials | None:
    """Read a Claude Code Keychain entry, or None when it's missing or locked."""
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
    if not oauth.get("accessToken"):
        return None
    return ClaudeCredentials(oauth=oauth, keychain_service=service)


def _keychain_account() -> str:
    """The Keychain item's account field. Claude Code stores credentials under
    the login name, and updating an item requires matching it."""
    return getpass.getuser()


def _security_quote(value: str) -> str:
    """Quote one argument for `security -i`, whose interactive mode re-parses
    each line with shell-like rules. The payload is JSON, so it is full of the
    quotes and backslashes that tokenizer treats as syntax."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _persist_credentials(creds: ClaudeCredentials) -> bool:
    """Write the blob back where it came from, so Claude Code and the dashboard
    keep sharing one credential. This matters most when the server rotates the
    refresh token: a new one we kept to ourselves would strand Claude Code
    holding a dead one."""
    blob = json.dumps({"claudeAiOauth": creds.oauth})
    if creds.file_path is not None:
        try:
            creds.file_path.write_text(blob)
            creds.file_path.chmod(0o600)
        except OSError:
            return False
        return True
    if not creds.keychain_service:
        return False
    # Fed over stdin rather than argv: a token in a command line is visible to
    # every process on the machine via `ps` for as long as the call runs.
    line = " ".join([
        "add-generic-password -U",
        "-a", _security_quote(_keychain_account()),
        "-s", _security_quote(creds.keychain_service),
        "-w", _security_quote(blob),
    ])
    try:
        out = subprocess.run(["security", "-i"], input=line + "\n",
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


def _post_json(url: str, body: dict, timeout: float = 10) -> dict | None:
    """POST a JSON body and decode the JSON reply, or None on any failure."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": CLAUDE_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _refresh_claude_token(creds: ClaudeCredentials) -> ClaudeCredentials | None:
    """Mint a fresh access token from the stored refresh token and persist the
    result. None when there's nothing to refresh with or the grant is refused,
    which means the account genuinely needs `claude auth login`."""
    if not creds.refresh_token:
        return None
    payload = _post_json(CLAUDE_OAUTH_TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": creds.refresh_token,
        "client_id": CLAUDE_OAUTH_CLIENT_ID,
    })
    access = (payload or {}).get("access_token")
    if not access:
        return None

    oauth = dict(creds.oauth)
    oauth["accessToken"] = access
    # Take the rotated refresh token whenever one comes back; keeping the old
    # one would leave the next refresh (ours or Claude Code's) holding a dud.
    if payload.get("refresh_token"):
        oauth["refreshToken"] = payload["refresh_token"]
    now = time.time()
    if payload.get("expires_in"):
        oauth["expiresAt"] = int((now + float(payload["expires_in"])) * 1000)
    if payload.get("refresh_token_expires_in"):
        oauth["refreshTokenExpiresAt"] = int((now + float(payload["refresh_token_expires_in"])) * 1000)
    if isinstance(payload.get("scope"), str):
        oauth["scopes"] = payload["scope"].split()

    refreshed = replace(creds, oauth=oauth)
    if not _persist_credentials(refreshed):
        # Non-fatal for this read, but the next process start would fall back to
        # the stale entry, so say so rather than failing silently.
        print("adash: refreshed a Claude token but could not write it back", file=sys.stderr)
    return refreshed


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
    # The model-scoped weekly limit (Fable on Max/Team) isn't a top-level key;
    # it only shows up in the `limits` array as a "weekly_scoped" entry tagged
    # with the model's display name. Surface it as its own window.
    for lim in payload.get("limits", []):
        if not isinstance(lim, dict) or lim.get("kind") != "weekly_scoped":
            continue
        name = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        if not name:
            continue
        pct = lim.get("percent")
        windows.append(
            UsageWindow(
                label=name.lower(),  # e.g. "fable"
                pct=float(pct) if pct is not None else None,
                resets_at=_parse_ts(lim.get("resets_at")),
            )
        )
        break  # one scoped limit is enough
    return windows


class RateLimited(Exception):
    """The usage endpoint returned 429. `retry_after` is its own advice in
    seconds when it sent a Retry-After header, else None."""

    def __init__(self, retry_after: float | None = None):
        super().__init__("rate limited")
        self.retry_after = retry_after


class Unauthorized(Exception):
    """The usage endpoint returned 401: the access token is expired, revoked,
    or was rotated out from under us. Worth exactly one refresh-and-retry."""


class AuthExpired(Exception):
    """The account can't be read without a human: no credentials, or a refresh
    the server refused. Carries the message shown on the row."""


@dataclass
class _ProfileCache:
    """Per-account fetch state: the last good reading and any active cooldown."""

    usage: ToolUsage | None = None
    fetched_at: float = 0.0  # time.monotonic() of that reading
    cooldown_until: float = 0.0  # don't call the endpoint again before this
    backoff: float = 0.0  # current 429 penalty, doubles per repeat
    # Why we're sitting out, as a marker template; "{wait}" (when present) is
    # filled with the time left so a rate limit can count itself down.
    cooldown_reason: str = ""


_claude_cache: dict[str, _ProfileCache] = {}


def _retry_after_seconds(headers) -> float | None:
    """Retry-After as seconds. The endpoint doesn't send one today, but honor
    it if that changes; an HTTP-date form is ignored in favor of our backoff."""
    try:
        return max(0.0, float(headers.get("Retry-After")))
    except (AttributeError, TypeError, ValueError):
        return None


def _short_delay(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds}s" if seconds < 60 else f"{round(seconds / 60)}m"


def _copy(usage: ToolUsage) -> ToolUsage:
    """Hand out a copy: callers mutate .label/.active, and the cache shouldn't see it."""
    return replace(usage, windows=list(usage.windows))


def _cooldown_marker(entry: _ProfileCache, now: float) -> str:
    """The stored reason, with any "{wait}" filled in from the time left. A rate
    limit counts itself down; a dead credential just says what to go do."""
    if "{wait}" in entry.cooldown_reason:
        return entry.cooldown_reason.format(wait=_short_delay(entry.cooldown_until - now))
    return entry.cooldown_reason


def _serve_cached(entry: _ProfileCache, marker: str, plan: str = "") -> ToolUsage:
    """Last known bars flagged with why they're old; a bare note if we never
    got a reading at all. Either way it stays inside the usage columns instead
    of spilling a raw exception across the row."""
    if entry.usage is not None:
        return replace(_copy(entry.usage), stale=marker)
    return ToolUsage(tool="claude", plan=plan, error=marker)


def _claude_usage_from_token(token: str, plan: str) -> ToolUsage:
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": CLAUDE_USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimited(_retry_after_seconds(exc.headers)) from exc
        if exc.code == 401:
            raise Unauthorized() from exc
        return ToolUsage(tool="claude", plan=plan, error=str(exc)[:60])
    except Exception as exc:  # network down, DNS, timeout
        return ToolUsage(tool="claude", plan=plan, error=str(exc)[:60])
    return ToolUsage(tool="claude", plan=plan, windows=_parse_claude_usage(payload))


SIGNED_OUT = "signed out · run claude auth login"


def _refreshed_or_signed_out(creds: ClaudeCredentials) -> ClaudeCredentials:
    fresh = _refresh_claude_token(creds)
    if fresh is None:
        raise AuthExpired(SIGNED_OUT)
    return fresh


def _read_claude_usage(profile: ClaudeProfile) -> ToolUsage:
    """One live reading for a profile, renewing the OAuth token when it needs
    it. Raises RateLimited or AuthExpired for the caller to turn into a
    cooldown; anything else comes back as a ToolUsage with .error set."""
    creds = _profile_credentials(profile)
    if creds is None:
        raise AuthExpired("unlock Keychain or sign in to Claude Code")

    # Claude Code only renews a token when it makes its own call, so an account
    # left idle has a dead one on disk. Spend the refresh, not a doomed request.
    if creds.expired():
        creds = _refreshed_or_signed_out(creds)

    try:
        return _claude_usage_from_token(creds.token, creds.plan)
    except Unauthorized:
        # The stored expiry lied: revoked, or rotated by another tool. One
        # refresh, one retry, then we accept that a human has to step in.
        creds = _refreshed_or_signed_out(creds)
        try:
            return _claude_usage_from_token(creds.token, creds.plan)
        except Unauthorized as exc:
            raise AuthExpired(SIGNED_OUT) from exc


def fetch_claude_usage_for(profile: ClaudeProfile, force: bool = False) -> ToolUsage:
    """Usage for one account, served from cache when that's good enough.

    `force` (the TUI's manual refresh) skips the freshness check but never a
    cooldown, so holding down the refresh key can't dig the hole deeper.
    """
    entry = _claude_cache.setdefault(str(profile.config_dir), _ProfileCache())
    now = time.monotonic()

    if not force and entry.usage is not None and now - entry.fetched_at < USAGE_CACHE_TTL_SECONDS:
        return _copy(entry.usage)
    if now < entry.cooldown_until:
        return _serve_cached(entry, _cooldown_marker(entry, now))

    try:
        usage = _read_claude_usage(profile)
    except RateLimited as exc:
        entry.backoff = min(max(entry.backoff * 2, RATE_LIMIT_BACKOFF_START), RATE_LIMIT_BACKOFF_MAX)
        wait = exc.retry_after if exc.retry_after is not None else entry.backoff
        entry.cooldown_reason = "rate limited · retry {wait}"
        entry.cooldown_until = now + wait
        return _serve_cached(entry, _cooldown_marker(entry, now))
    except AuthExpired as exc:
        entry.cooldown_reason = str(exc)
        entry.cooldown_until = now + AUTH_FAILURE_COOLDOWN_SECONDS
        return _serve_cached(entry, str(exc))

    if usage.error:  # transient (network, DNS): retry next poll, don't cache
        return _serve_cached(entry, usage.error, plan=usage.plan)
    entry.usage, entry.fetched_at = usage, now
    entry.backoff, entry.cooldown_until, entry.cooldown_reason = 0.0, 0.0, ""
    return _copy(usage)


def fetch_claude_usages(active_label: str | None = None, force: bool = False) -> list[ToolUsage]:
    """One ToolUsage per Claude account. `label` is set (and the active plan
    flagged) only when more than one account exists, so single-account setups
    render exactly as before."""
    profiles = claude_profiles()
    usages = []
    for profile in profiles:
        usage = fetch_claude_usage_for(profile, force=force)
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


def collect_usage(active_claude: str | None = None, force: bool = False) -> tuple[list[ToolUsage], datetime]:
    usages = [*fetch_claude_usages(active_claude, force=force), fetch_codex_usage(), fetch_opencode_usage()]
    return usages, datetime.now(timezone.utc)
