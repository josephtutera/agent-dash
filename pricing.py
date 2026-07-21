"""Value at API rates: what flat-rate subscription usage would have cost à la carte.

Joseph pays flat monthly subscriptions (Claude Team, Claude Personal, Codex),
so the tokens themselves are already paid for. This module answers a different
question: if that same usage had run through the metered API instead, what would
the bill have been? Comparing that estimate to the subscription price gives a
"multiple" (how many times over the sub has paid for itself), the same headline
the OpenUsage app shows.

Everything here is an estimate at published per-token API rates, never a real
invoice. Rounding, undocumented discounts, and model aliasing all move the number.

Three layers:
  1. A pricing table from LiteLLM's public price list, cached on disk for 24h,
     with a trimmed snapshot committed to the repo as an offline fallback.
  2. Log scanners that read the same local session files the rest of adash reads
     (Claude projects/*.jsonl, Codex sessions/rollout-*.jsonl) and turn token
     buckets into dollars. Results are cached per file by mtime+size, so only
     changed files are re-read.
  3. collect_value(), which rolls the priced records up into today / this-month
     totals per subscription and against the configured monthly cost.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:  # tomllib is stdlib from 3.11; on 3.10 we simply can't read the sub costs
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter version
    tomllib = None

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
PRICES_CACHE_TTL_SECONDS = 24 * 60 * 60  # refresh the live price list at most once a day
PRICING_CACHE_VERSION = 1  # bump to invalidate the per-file scan cache on format changes

# When a Codex rollout never names its model (no turn_context event), fall back
# to this so real spend still lands somewhere priceable rather than being dropped.
DEFAULT_CODEX_MODEL = "gpt-5.6"

# subscription ids used throughout the report and the config file
SUB_CLAUDE_TEAM = "claude-team"
SUB_CLAUDE_PERSONAL = "claude-personal"
SUB_CODEX = "codex"


# ---------------------------------------------------------------- pricing table


@dataclass
class ModelRates:
    """Per-token USD rates for one model, normalized across providers."""

    input: float
    output: float
    cache_creation: float
    cache_read: float


def _rates_from_entry(entry: dict) -> ModelRates | None:
    """Pull the four rates out of a LiteLLM entry, filling cache rates from the
    input rate when the table doesn't list them (prefer the table's own keys)."""
    inp = entry.get("input_cost_per_token")
    if inp is None:
        return None
    out = entry.get("output_cost_per_token") or 0.0
    creation = entry.get("cache_creation_input_token_cost")
    read = entry.get("cache_read_input_token_cost")
    if creation is None:
        creation = inp  # writing a cache entry costs about a fresh input token
    if read is None:
        read = 0.1 * inp  # cache reads are ~10% of input across both providers
    return ModelRates(input=float(inp), output=float(out), cache_creation=float(creation), cache_read=float(read))


def _prices_cache_path(cache_dir: Path | None) -> Path:
    cache_dir = cache_dir or Path.home() / ".cache" / "adash"
    return cache_dir / "model-prices.json"


def _snapshot_path() -> Path:
    return Path(__file__).parent / "data" / "model_prices_snapshot.json"


def _fetch_live_prices(timeout: float = 8.0) -> dict | None:
    try:
        req = urllib.request.Request(LITELLM_URL, headers={"User-Agent": "adash"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None  # any network/parse trouble silently falls back to cache/snapshot


def load_price_table(cache_dir: Path | None = None, now: float | None = None) -> dict:
    """Return the raw {model: entry} price map.

    Freshest source first: an on-disk cache younger than 24h is used as-is; older
    than that (or missing) we try the live LiteLLM list and rewrite the cache;
    if the network is down we fall back to the stale cache, then to the bundled
    snapshot. Nothing here raises, so callers always get *some* table.
    """
    cache_dir = cache_dir or Path.home() / ".cache" / "adash"
    now = time.time() if now is None else now
    cache_path = _prices_cache_path(cache_dir)

    try:
        age = now - cache_path.stat().st_mtime
    except OSError:
        age = None

    if age is not None and age < PRICES_CACHE_TTL_SECONDS:
        cached = _read_json(cache_path)
        if cached:
            return cached

    live = _fetch_live_prices()
    if live:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with cache_path.open("w") as fh:
                json.dump(live, fh)
        except OSError:
            pass
        return live

    cached = _read_json(cache_path)  # network failed: any cache beats the snapshot
    if cached:
        return cached
    return _read_json(_snapshot_path()) or {}


def _read_json(path: Path) -> dict | None:
    try:
        with path.open() as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


class PriceResolver:
    """Resolves messy model ids to rates and remembers which ones it couldn't.

    Names in the logs don't always match the table verbatim: Codex emits internal
    aliases like "gpt-5.6-terra", providers prefix ids with "anthropic/", and a
    dated id ("claude-opus-4-8-20260101") may only appear undated in the table.
    We try exact match, then strip a provider prefix, then peel trailing
    "-segment" suffixes one at a time until something matches. Anything that never
    resolves is recorded in `skipped` so it's reported, never silently free.
    """

    def __init__(self, table: dict):
        self._table = table
        self._cache: dict[str, ModelRates | None] = {}
        self.skipped: set[str] = set()

    def rates_for(self, model: str) -> ModelRates | None:
        if not model:
            self.skipped.add("(unknown)")
            return None
        if model in self._cache:
            rates = self._cache[model]
            if rates is None:
                self.skipped.add(model)
            return rates
        rates = self._resolve(model)
        self._cache[model] = rates
        if rates is None:
            self.skipped.add(model)
        return rates

    def _resolve(self, model: str) -> ModelRates | None:
        for candidate in self._candidates(model):
            entry = self._table.get(candidate)
            if isinstance(entry, dict):
                rates = _rates_from_entry(entry)
                if rates:
                    return rates
        return None

    @staticmethod
    def _candidates(model: str):
        seen: set[str] = set()
        forms = [model, model.lower()]
        if "/" in model:  # drop a provider prefix like "anthropic/" or "openai/"
            forms.append(model.split("/", 1)[1])
            forms.append(model.split("/", 1)[1].lower())
        for form in forms:
            # peel one trailing hyphen-segment at a time: gpt-5.6-terra -> gpt-5.6
            parts = form.split("-")
            for cut in range(len(parts), 0, -1):
                candidate = "-".join(parts[:cut])
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    yield candidate


# ---------------------------------------------------------------- token buckets


@dataclass
class _Buckets:
    """Four token counts, normalized so both providers price identically."""

    input: int = 0  # non-cached input tokens
    output: int = 0  # output including reasoning tokens
    cache_creation: int = 0  # tokens written into the prompt cache
    cache_read: int = 0  # tokens served from the prompt cache

    def add(self, other: "_Buckets") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_creation += other.cache_creation
        self.cache_read += other.cache_read

    def cost(self, rates: ModelRates) -> float:
        return (
            self.input * rates.input
            + self.output * rates.output
            + self.cache_creation * rates.cache_creation
            + self.cache_read * rates.cache_read
        )


def _local_date(ts_iso: str | None) -> str | None:
    """ISO timestamp (with Z or offset) -> 'YYYY-MM-DD' in the machine's local tz."""
    if not isinstance(ts_iso, str) or not ts_iso:
        return None
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d")


# A parsed file becomes a flat list of [date, model, input, output, cc, cr] rows
# plus a list of [date, usd] explicit-cost rows. Both are JSON-friendly so they
# cache verbatim, and pricing is applied fresh each run (a price refresh needs no
# rescan). Rows are pre-aggregated per (date, model) within a file to stay small.
_ParsedFile = dict


def _aggregate_rows(by_key: dict[tuple[str, str], _Buckets]) -> list:
    return [
        [date, model, b.input, b.output, b.cache_creation, b.cache_read]
        for (date, model), b in by_key.items()
    ]


# ---------------------------------------------------------------- claude scanner

_CLAUDE_NEEDLES = ('"usage"', '"costUSD"')


def _parse_claude_file(path: Path) -> _ParsedFile:
    """Aggregate one Claude transcript's assistant token usage by (date, model).

    Claude's usage buckets are already disjoint (input/output/cache_creation/
    cache_read never overlap), so they map straight onto _Buckets. A line that
    carries an explicit costUSD is trusted over the token math for that line.
    """
    rows: dict[tuple[str, str], _Buckets] = {}
    costs: dict[str, float] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not any(needle in line for needle in _CLAUDE_NEEDLES):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                date = _local_date(obj.get("timestamp"))
                if date is None:
                    continue
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                if obj.get("costUSD") is not None:  # explicit cost wins over token math
                    try:
                        costs[date] = costs.get(date, 0.0) + float(obj["costUSD"])
                    except (TypeError, ValueError):
                        pass
                    continue
                usage = message.get("usage") or {}
                model = message.get("model") or ""
                bucket = _Buckets(
                    input=int(usage.get("input_tokens") or 0),
                    output=int(usage.get("output_tokens") or 0),
                    cache_creation=int(usage.get("cache_creation_input_tokens") or 0),
                    cache_read=int(usage.get("cache_read_input_tokens") or 0),
                )
                rows.setdefault((date, model), _Buckets()).add(bucket)
    except OSError:
        return {"rows": [], "costs": []}
    return {"rows": _aggregate_rows(rows), "costs": [[d, c] for d, c in costs.items()]}


def _claude_account_dirs(home: Path | None = None) -> list[tuple[str, Path]]:
    """(subscription_id, projects_dir) for each Claude account with sessions.

    The default ~/.claude is the Team account; each sibling ~/.claude-<name>
    maps to a "claude-<name>" id, so ~/.claude-personal -> "claude-personal".
    """
    home = Path(home) if home else Path.home()
    out: list[tuple[str, Path]] = []
    default = home / ".claude" / "projects"
    if default.is_dir():
        out.append((SUB_CLAUDE_TEAM, default))
    for d in sorted(home.glob(".claude-*")):
        projects = d / "projects"
        if projects.is_dir():
            suffix = d.name[len(".claude-"):] or d.name
            out.append((f"claude-{suffix}", projects))
    return out


# ---------------------------------------------------------------- codex scanner

_CODEX_NEEDLES = ("token_count", "session_meta", "turn_context")


def _parse_codex_file(path: Path) -> _ParsedFile:
    """Aggregate one Codex rollout's per-turn token usage by (date, model).

    Codex writes a cumulative `total_token_usage` that balloons because cache
    reads are re-counted every turn, so it is useless for pricing. Instead we
    price each turn's `last_token_usage` (the delta for that turn) at the model
    named by the most recent turn_context event. Codex's `input_tokens` already
    includes the cached slice, so we subtract `cached_input_tokens` back out to
    get the non-cached input and price the cached part at the cache-read rate.

    Subagent/forked rollouts share a parent's session_id; they are marked with a
    non-"user" thread_source in session_meta and skipped, exactly as the session
    collector does, so their tokens aren't double-counted. Resumed user sessions
    live in separate files whose turns are time-disjoint, so summing all of a
    session's user files never double-counts.
    """
    rows: dict[tuple[str, str], _Buckets] = {}
    current_model = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not any(needle in line for needle in _CODEX_NEEDLES):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                if obj.get("type") == "session_meta":
                    if payload.get("thread_source") not in (None, "user"):
                        return {"rows": [], "costs": []}  # subagent rollout: skip whole file
                    continue
                ptype = payload.get("type")
                if ptype == "turn_context":
                    model = payload.get("model")
                    if isinstance(model, str) and model:
                        current_model = model
                    continue
                if ptype != "token_count":
                    continue
                last = (payload.get("info") or {}).get("last_token_usage")
                if not isinstance(last, dict):
                    continue
                date = _local_date(obj.get("timestamp"))
                if date is None:
                    continue
                input_tokens = int(last.get("input_tokens") or 0)
                cached = int(last.get("cached_input_tokens") or 0)
                bucket = _Buckets(
                    input=max(0, input_tokens - cached),  # codex input_tokens includes the cached slice
                    output=int(last.get("output_tokens") or 0),  # already includes reasoning tokens
                    cache_creation=int(last.get("cache_write_input_tokens") or 0),
                    cache_read=cached,
                )
                model = current_model or DEFAULT_CODEX_MODEL
                rows.setdefault((date, model), _Buckets()).add(bucket)
    except OSError:
        return {"rows": [], "costs": []}
    return {"rows": _aggregate_rows(rows), "costs": []}


def _codex_session_dirs(home: Path | None = None) -> list[Path]:
    home = Path(home) if home else Path.home()
    root = home / ".codex"
    dirs = []
    for name in ("sessions", "archived_sessions"):
        d = root / name
        if d.is_dir():
            dirs.append(d)
    return dirs


# ---------------------------------------------------------------- scan + cache


def _parse_with_cache(files: list[Path], parse_fn, cache: dict) -> list[_ParsedFile]:
    """Parse each file, reusing the cached result when mtime+size are unchanged.

    Mirrors collectors._parse_files but stores our priced-token rows instead of a
    Session, and stays single-threaded: the newest Codex rollouts run to hundreds
    of MB, and the win here is skipping unchanged files entirely, not parallelism.
    """
    results: list[_ParsedFile] = []
    current: set[str] = set()
    for path in files:
        key = str(path)
        current.add(key)
        try:
            stat = path.stat()
        except OSError:
            continue
        entry = cache.get(key)
        if entry and entry.get("mtime") == stat.st_mtime and entry.get("size") == stat.st_size:
            results.append(entry["parsed"])
            continue
        parsed = parse_fn(path)
        cache[key] = {"mtime": stat.st_mtime, "size": stat.st_size, "parsed": parsed}
        results.append(parsed)
    for key in [k for k in cache if k not in current]:  # forget files that vanished
        del cache[key]
    return results


def _pricing_cache_file(cache_dir: Path | None) -> Path:
    cache_dir = cache_dir or Path.home() / ".cache" / "adash"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "pricing-cache.json"


def _load_scan_cache(cache_dir: Path | None) -> dict:
    try:
        with _pricing_cache_file(cache_dir).open() as fh:
            data = json.load(fh)
        if data.get("version") == PRICING_CACHE_VERSION:
            return {"claude": data.get("claude", {}), "codex": data.get("codex", {})}
    except (OSError, ValueError):
        pass
    return {"claude": {}, "codex": {}}


def _save_scan_cache(cache_dir: Path | None, cache: dict) -> None:
    try:
        with _pricing_cache_file(cache_dir).open("w") as fh:
            json.dump({"version": PRICING_CACHE_VERSION, **cache}, fh)
    except OSError:
        pass


def _price_parsed(parsed_files: list[_ParsedFile], resolver: PriceResolver) -> dict[str, float]:
    """Collapse parsed files into {date: usd}, pricing token rows and adding any
    explicit costs. Unpriceable models are skipped (and recorded by the resolver)."""
    by_date: dict[str, float] = {}
    for parsed in parsed_files:
        for row in parsed.get("rows", []):
            date, model, inp, out, cc, cr = row
            rates = resolver.rates_for(model)
            if rates is None:
                continue
            bucket = _Buckets(input=inp, output=out, cache_creation=cc, cache_read=cr)
            by_date[date] = by_date.get(date, 0.0) + bucket.cost(rates)
        for date, usd in parsed.get("costs", []):
            by_date[date] = by_date.get(date, 0.0) + float(usd)
    return by_date


# ---------------------------------------------------------------- config


def _config_path(home: Path | None = None) -> Path:
    home = Path(home) if home else Path.home()
    return home / ".config" / "adash" / "config.toml"


def load_subscription_costs(home: Path | None = None) -> dict[str, float]:
    """Read [subscriptions] id -> monthly_cost_usd from the config file.

    Returns {} when the file is missing, unreadable, or tomllib is unavailable
    (Python 3.10); the report then leaves subscription cost and the multiple
    unknown rather than guessing.
    """
    if tomllib is None:
        return {}
    path = _config_path(home)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}
    subs = data.get("subscriptions")
    if not isinstance(subs, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in subs.items():
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- aggregation


@dataclass
class SubValue:
    """One subscription's estimated à la carte value for today and this month."""

    id: str
    today_usd: float = 0.0
    month_usd: float = 0.0
    subs_cost_usd: float | None = None  # configured monthly price, if known
    multiple: float | None = None  # month_usd / subs_cost_usd when both known


@dataclass
class ValueReport:
    """API-rate value estimate across all subscriptions. Never a real bill."""

    subs: list[SubValue] = field(default_factory=list)
    today_total_usd: float = 0.0
    month_total_usd: float = 0.0
    subs_cost_usd: float | None = None  # sum of configured monthly prices
    multiple: float | None = None  # month_total / subs_cost when both known
    skipped_models: set[str] = field(default_factory=set)
    generated_at: datetime | None = None

    def sub(self, sub_id: str) -> SubValue | None:
        return next((s for s in self.subs if s.id == sub_id), None)


def _sub_from_dates(sub_id: str, by_date: dict[str, float], today: str, month_prefix: str) -> SubValue:
    today_usd = by_date.get(today, 0.0)
    month_usd = sum(usd for date, usd in by_date.items() if date.startswith(month_prefix))
    return SubValue(id=sub_id, today_usd=today_usd, month_usd=month_usd)


def collect_value(
    now: datetime | None = None,
    home: Path | None = None,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> ValueReport:
    """Build the full value report: per-subscription today / month-to-date value
    at API rates, the configured monthly cost, and the multiple over that cost.

    `now` fixes "today" and the calendar month (local time) for tests; it
    defaults to the current local time. Both Claude accounts and Codex are
    scanned from local logs; the price table and per-file parse are cached.
    """
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    today = now.strftime("%Y-%m-%d")
    month_prefix = now.strftime("%Y-%m")

    table = load_price_table(cache_dir=cache_dir)
    resolver = PriceResolver(table)

    scan_cache = _load_scan_cache(cache_dir) if use_cache else {"claude": {}, "codex": {}}

    # ---- Claude: one SubValue per account dir ----
    subs: list[SubValue] = []
    for sub_id, projects_dir in _claude_account_dirs(home):
        files = sorted(projects_dir.glob("*/*.jsonl"))
        parsed = _parse_with_cache(files, _parse_claude_file, scan_cache["claude"])
        by_date = _price_parsed(parsed, resolver)
        subs.append(_sub_from_dates(sub_id, by_date, today, month_prefix))

    # ---- Codex: one SubValue across all session dirs ----
    codex_files: list[Path] = []
    for d in _codex_session_dirs(home):
        codex_files.extend(d.glob("**/rollout-*.jsonl"))
    codex_files.sort()
    codex_parsed = _parse_with_cache(codex_files, _parse_codex_file, scan_cache["codex"])
    codex_by_date = _price_parsed(codex_parsed, resolver)
    subs.append(_sub_from_dates(SUB_CODEX, codex_by_date, today, month_prefix))

    if use_cache:
        _save_scan_cache(cache_dir, scan_cache)

    # ---- fold in configured subscription costs ----
    sub_costs = load_subscription_costs(home)
    for sub in subs:
        cost = sub_costs.get(sub.id)
        if cost is not None:
            sub.subs_cost_usd = cost
            if cost > 0:
                sub.multiple = sub.month_usd / cost

    today_total = sum(s.today_usd for s in subs)
    month_total = sum(s.month_usd for s in subs)
    total_cost = sum(sub_costs.values()) if sub_costs else None
    multiple = month_total / total_cost if total_cost else None

    return ValueReport(
        subs=subs,
        today_total_usd=today_total,
        month_total_usd=month_total,
        subs_cost_usd=total_cost,
        multiple=multiple,
        skipped_models=set(resolver.skipped),
        generated_at=now,
    )


def _round2(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def hud_value(report: ValueReport | None = None) -> dict | None:
    """The daemon-facing HUD contract: a JSON-safe dict of plain primitives.

    ValueReport itself carries a set (skipped_models) and a datetime
    (generated_at) for the CLI, and plain json.dumps chokes on both. The HUD
    daemon and the Swift app consume this flattened shape instead, so this is the
    one public surface they depend on. Every value here is a float, a str, or
    None; dollars are rounded to cents. Keys are exactly:

        today_usd, month_usd, subs_cost_usd, multiple,
        by_sub -> {<sub id>: {today_usd, month_usd}}

    Passing `report=None` builds a fresh report via collect_value().
    """
    if report is None:
        report = collect_value()
    return {
        "today_usd": _round2(report.today_total_usd),
        "month_usd": _round2(report.month_total_usd),
        "subs_cost_usd": _round2(report.subs_cost_usd),
        "multiple": _round2(report.multiple),
        "by_sub": {
            sub.id: {"today_usd": _round2(sub.today_usd), "month_usd": _round2(sub.month_usd)}
            for sub in report.subs
        },
    }
