# HUD snapshot schema (v1)

The `adash serve` daemon maintains one JSON snapshot of subscription usage and
live agent activity. It is written atomically to `~/.cache/adash/hud.json` on
every meaningful change and served over a loopback HTTP API. This document is the
contract for the Swift HUD app: the field names below are frozen for v1 and will
not be renamed.

## Serving it

- `GET /v1/hud` returns the snapshot as `application/json`.
- `GET /v1/health` returns `{"ok": true, "version": 1}`.
- Any other path returns `404`.
- The server binds loopback only (default `127.0.0.1:8737`). CORS is permissive
  (`Access-Control-Allow-Origin: *`) because only localhost can reach it anyway.
- Because the API has no authentication, a non-loopback `--host` (anything other
  than `127.0.0.1`, `::1`, or `localhost`) is refused with a clear error. Set the
  environment variable `ADASH_SERVE_ALLOW_REMOTE=1` to override at your own risk.
- No credentials, tokens, or file paths beyond the agent working directory ever
  appear in the snapshot.

Run it with `adash serve` (or `adash --serve`), optionally with `--host` and
`--port`.

## Polling

- Subscription usage polls every 180 seconds, always through usage.py's cache and
  429 backoff, because the Anthropic usage endpoint is rate-limited per account
  and shared with every live Claude Code session. This daemon is meant to be the
  single resident poller.
- Live activity (running agents and their state) polls every 2 seconds, since it
  is cheap on-disk reads plus one `ps`.

## Top-level shape

```json
{
  "version": 1,
  "generated_at": "2026-07-21T18:04:05.123456+00:00",
  "subscriptions": [ ... ],
  "agents": [ ... ],
  "value": { ... } | null,
  "soonest_reset": { ... } | null
}
```

| field | type | meaning |
|---|---|---|
| `version` | int | Schema version. Always `1` for this contract. |
| `generated_at` | ISO8601 string with offset | When this snapshot was built (UTC). |
| `subscriptions` | array | One entry per Claude account plus Codex. OpenCode is BYOK, not a subscription, so it never appears here. |
| `agents` | array | Every running claude / codex / opencode terminal session detected right now. |
| `value` | object or null | Dollar value delivered vs. subscription cost. `null` when the pricing collector is unavailable. |
| `soonest_reset` | object or null | The single earliest-resetting window across all subscriptions, or `null` when no window reports a reset time. |

## `subscriptions[]`

```json
{
  "id": "claude-team",
  "provider": "claude",
  "label": "Claude Team",
  "windows": [ ... ],
  "tightest": { "kind": "session_5h", "pct_left": 62, "resets_at": "..." } | null,
  "stale": null,
  "active_agents": 2
}
```

| field | type | meaning |
|---|---|---|
| `id` | string | Stable id. `claude-team` is the built-in `~/.claude`; `claude-personal` is `~/.claude-personal`; any other `~/.claude-<name>` becomes `claude-<name>`; Codex is `codex`. |
| `provider` | `"claude"` \| `"codex"` | Which vendor this subscription is. |
| `label` | string | Display name, e.g. `Claude Team`, `Claude Personal`, `Codex Max`. |
| `windows` | array | The usage windows this subscription reports (see below). |
| `tightest` | object or null | The window with the least headroom (lowest `pct_left`), copied out for quick access. `null` when no window has a percentage. Carries `kind`, `pct_left`, `resets_at`. |
| `stale` | string or null | `null` when the reading is fresh. A human reason like `"rate limited, retry 4m"` when the values are last-good rather than current (rate limit cooldown or a fetch failure). Stale data is never presented as fresh: the windows keep their last-good numbers and this field says why. |
| `active_agents` | int | How many entries in `agents[]` are attributed to this subscription. |

### `subscriptions[].windows[]`

```json
{
  "kind": "session_5h",
  "pct_left": 62,
  "resets_at": "2026-07-21T22:00:00+00:00",
  "pace": { "projected_dry_at": "2026-07-21T21:10:00+00:00", "margin_seconds": -3000 } | null
}
```

| field | type | meaning |
|---|---|---|
| `kind` | string | The window type. Claude reports `session_5h` (the 5-hour session limit), `weekly_7d` (the 7-day limit), and `weekly_fable` (the model-scoped weekly limit, Fable on Max/Team). Codex reports `session_5h` and `weekly`. |
| `pct_left` | int 0-100, or null | Percent of the quota still available (`100 - utilization`). `null` when the subscription doesn't report a percentage for this window. |
| `resets_at` | ISO8601 string, or null | When the window rolls over. `null` when unknown. |
| `pace` | object or null | A linear burn projection, computed only for the subscription's tightest window and only when computable (needs a reset time and some usage). `null` on every other window. |

`pace.projected_dry_at` is when the quota would hit zero if the average burn since
the window opened continued. `pace.margin_seconds` is how many seconds of slack
that leaves before `resets_at`: positive means the window resets before you run
dry (safe), negative means you would run out first at the current pace.

## `agents[]`

```json
{
  "pid": 59001,
  "tool": "claude",
  "project": "web-app",
  "cwd": "/Users/you/Repos/web-app",
  "state": "working",
  "action": "editing auth.py",
  "since_seconds": 720,
  "subscription_id": "claude-team"
}
```

| field | type | meaning |
|---|---|---|
| `pid` | int | Process id of the terminal agent session. |
| `tool` | `"claude"` \| `"codex"` \| `"opencode"` | Which CLI this is. |
| `project` | string | Basename of the working directory. |
| `cwd` | string | Full working directory path. |
| `state` | `"working"` \| `"waiting"` \| `"idle"` | Live status. `waiting` means it is blocked on the user (e.g. a permission prompt). An unknown status is reported as `idle`. |
| `action` | string or null | The current action text (e.g. `editing auth.py`) when known, else `null`. |
| `since_seconds` | int or null | Approximate session uptime in seconds, or `null` when not derivable. |
| `subscription_id` | string or null | The subscription this agent spends against, when determinable. Codex agents are always `codex`; a Claude agent is matched to the account whose config dir holds its per-pid session file; opencode and unresolved agents are `null`. |

## `value`

```json
{
  "today_usd": 42.10,
  "month_usd": 830.55,
  "subs_cost_usd": 400.0,
  "multiple": 2.08,
  "by_sub": { "claude-team": { "today_usd": 30.0, "month_usd": 600.0 } }
}
```

`null` as a whole when the pricing module is not present on this build, or when
its output cannot be JSON-serialized (the daemon validates the block and drops it
rather than crash). When present, the daemon prefers `pricing.hud_value()`, which
returns exactly this contract; older builds exposing only `collect_value()` are
coerced down to it. The block estimates the API-equivalent dollar value of the
work done today and this month, the flat monthly subscription cost
(`subs_cost_usd`, may be `null`), the value-to-cost `multiple` (may be `null`),
and a per-subscription breakdown keyed by subscription id.

## `soonest_reset`

```json
{ "subscription_id": "claude-team", "kind": "session_5h", "resets_at": "2026-07-21T22:00:00+00:00" }
```

The earliest upcoming window reset across every subscription, so the HUD can show
a single "next reset" without walking the whole tree. `null` when nothing reports
a reset time.
