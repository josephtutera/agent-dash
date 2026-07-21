"""Collect sessions from Claude Code, Codex, and OpenCode local storage.

Each collector is defensive: malformed lines and missing files are skipped,
because these on-disk formats change between tool versions.

Parsing results are cached on disk (keyed by file path + mtime + size), so
repeated launches only re-parse files that changed.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from models import Session, clean_title

DEFAULT_LIMIT = 300
CACHE_VERSION = 4


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# prefixes that mark content a human never typed (injected context blocks)
_SKIP_PREFIXES = (
    "<",
    "# AGENTS.md",
    "Base directory for this skill:",
    "Caveat:",
    "[Request interrupted",
    "Warmup",
    "The previous response failed to produce a valid tool call",
)

_COMMAND_RE = re.compile(r"<command-name>([^<]+)</command-name>")
_COMMAND_ARGS_RE = re.compile(r"<command-args>([^<]*)</command-args>")


def _command_title(content) -> str | None:
    """Turn a slash-command invocation into a readable title: '/review <args>'."""
    if not isinstance(content, str):
        return None
    match = _COMMAND_RE.search(content)
    if not match:
        return None
    name = match.group(1).strip()
    args_match = _COMMAND_ARGS_RE.search(content)
    args = " ".join(args_match.group(1).split()) if args_match else ""
    title = f"{name} {args}".strip()
    return title[:70] + "…" if len(title) > 70 else title


def _user_text(content) -> str | None:
    """Extract text a human actually typed; None for tool results or injected blocks."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") in ("text", "input_text")
        ]
        text = "\n".join(part for part in parts if part)
    else:
        return None
    text = " ".join(text.split())
    if not text or text.startswith(_SKIP_PREFIXES):
        return None
    return text


def _session_to_dict(session: Session) -> dict:
    data = dataclasses.asdict(session)
    data["last_active"] = session.last_active.isoformat()
    return data


def _session_from_dict(data: dict) -> Session:
    data = dict(data)
    data["last_active"] = datetime.fromisoformat(data["last_active"])
    return Session(**data)


# ---------------------------------------------------------------- claude

# only lines containing one of these substrings get a full json parse
# (both compact and spaced json variants, in case a tool changes formatting)
_CLAUDE_NEEDLES = ('"usage"', '"type":"user"', '"type": "user"', "aiTitle", '"cwd"')


def _parse_claude_file(path: Path) -> Session | None:
    session_id = path.stem
    title = None
    command = None
    cwd = None
    first_prompt = ""
    n_messages = 0
    tokens = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not any(needle in line for needle in _CLAUDE_NEEDLES):
                    continue
                if '"tool_result"' in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                kind = obj.get("type")
                if kind == "ai-title" and title is None:
                    candidate = obj.get("aiTitle")
                    if isinstance(candidate, str) and candidate.strip():
                        title = candidate.strip()
                if cwd is None and isinstance(obj.get("cwd"), str):
                    cwd = obj["cwd"]
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                if kind == "assistant":
                    usage = message.get("usage") or {}
                    tokens += sum(
                        int(usage.get(key) or 0)
                        for key in (
                            "input_tokens",
                            "output_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                        )
                    )
                    n_messages += 1
                elif kind == "user":
                    if command is None:
                        command = _command_title(message.get("content"))
                    text = _user_text(message.get("content"))
                    if text is not None:
                        n_messages += 1
                        if not first_prompt:
                            first_prompt = text
    except OSError:
        return None
    last = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    if not title:
        # a bare command like "/model" says nothing about which session this is,
        # so a real prompt typed afterwards wins. a command *with* args already
        # describes the work ("/review 412"), so that keeps precedence.
        if command and (not first_prompt or " " in command):
            title = command
        elif first_prompt:
            title = clean_title(first_prompt)
        else:
            title = command or "(untitled)"
    return Session(
        tool="claude",
        id=session_id,
        title=title,
        project_dir=cwd or "",
        last_active=last,
        n_messages=n_messages,
        tokens=tokens,
        first_prompt=first_prompt,
    )


def collect_claude(root: Path | None = None, limit: int = DEFAULT_LIMIT, cache: dict | None = None) -> list[Session]:
    root = root or Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return []
    files = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return _parse_files(files, _parse_claude_file, cache)


# ---------------------------------------------------------------- codex

_CODEX_NEEDLES = ("session_meta", "token_count", "user_message", '"role":"user"', '"role": "user"', '"role":"assistant"', '"role": "assistant"')


def _load_codex_index(root: Path) -> dict[str, str]:
    """session_index.jsonl maps thread id -> human thread name."""
    index: dict[str, str] = {}
    index_file = root / "session_index.jsonl"
    if index_file.is_file():
        with index_file.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                sid, name = obj.get("id"), obj.get("thread_name")
                if sid and name:
                    index[sid] = name
    return index


def _parse_codex_file(path: Path) -> Session | None:
    session_id = None
    cwd = None
    tokens = 0
    n_messages = 0
    first_prompt = ""
    is_user_thread = True
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
                kind = obj.get("type")
                if kind == "session_meta":
                    # subagent rollouts share the parent's session_id; skip them
                    if payload.get("thread_source") not in (None, "user"):
                        is_user_thread = False
                        break
                    session_id = payload.get("session_id") or payload.get("id")
                    cwd = payload.get("cwd")
                elif kind == "event_msg" and payload.get("type") == "token_count":
                    total = (payload.get("info") or {}).get("total_token_usage") or {}
                    # token_count events are cumulative, so the last one is the file's total
                    if total.get("total_tokens"):
                        tokens = int(total["total_tokens"])
                elif kind == "event_msg" and payload.get("type") == "user_message":
                    # Codex 0.145 records the human's actual message here;
                    # response_item user messages are now mostly injected context.
                    text = _user_text(payload.get("message"))
                    if text is not None:
                        n_messages += 1
                        if not first_prompt:
                            first_prompt = text
                elif kind == "response_item" and payload.get("type") == "message":
                    role = payload.get("role")
                    if role == "user":
                        text = _user_text(payload.get("content"))
                        if text is not None:
                            n_messages += 1
                            if not first_prompt:
                                first_prompt = text
                    elif role == "assistant":
                        n_messages += 1
    except OSError:
        return None
    if not is_user_thread or session_id is None:
        return None
    last = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return Session(
        tool="codex",
        id=session_id,
        title="",  # resolved from the thread index after dedup
        project_dir=cwd or "",
        last_active=last,
        n_messages=n_messages,
        tokens=tokens,
        first_prompt=first_prompt,
    )


def collect_codex(root: Path | None = None, limit: int = DEFAULT_LIMIT, cache: dict | None = None) -> list[Session]:
    root = root or Path.home() / ".codex"
    sessions_dir = root / "sessions"
    if not sessions_dir.is_dir():
        return []
    # overscan: many rollout files are subagent spawns that get filtered out
    files = sorted(sessions_dir.glob("**/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[: limit * 4]
    parsed = _parse_files(files, _parse_codex_file, cache)

    # resumed threads produce multiple rollout files sharing one session_id;
    # merge them into a single row (newest file wins, stats accumulate)
    threads: dict[str, Session] = {}
    earliest_prompt: dict[str, tuple[datetime, str]] = {}
    for session in parsed:
        if session.first_prompt:
            current = earliest_prompt.get(session.id)
            if current is None or session.last_active < current[0]:
                earliest_prompt[session.id] = (session.last_active, session.first_prompt)
        existing = threads.get(session.id)
        if existing is None:
            threads[session.id] = session
            continue
        existing.tokens = max(existing.tokens, session.tokens)
        existing.n_messages += session.n_messages
        if session.last_active > existing.last_active:
            session.tokens, session.n_messages = existing.tokens, existing.n_messages
            threads[session.id] = session

    index = _load_codex_index(root)
    sessions = sorted(threads.values(), key=lambda s: s.last_active, reverse=True)[:limit]
    for session in sessions:
        if not session.first_prompt and session.id in earliest_prompt:
            session.first_prompt = earliest_prompt[session.id][1]
        title = index.get(session.id)
        if not title:
            title = clean_title(session.first_prompt) if session.first_prompt else "(untitled)"
        session.title = title
    return sessions


# ---------------------------------------------------------------- opencode


def collect_opencode(db_path: Path | None = None, limit: int = DEFAULT_LIMIT) -> list[Session]:
    db_path = db_path or Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if not db_path.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                """
                SELECT id, title, directory,
                       tokens_input, tokens_output, tokens_reasoning,
                       cost, time_updated
                FROM session
                WHERE time_archived IS NULL
                ORDER BY time_updated DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    sessions = []
    for sid, title, directory, t_in, t_out, t_reasoning, cost, updated in rows:
        sessions.append(
            Session(
                tool="opencode",
                id=sid,
                title=title or "(untitled)",
                project_dir=directory or "",
                last_active=datetime.fromtimestamp(updated / 1000, tz=timezone.utc),
                tokens=(t_in or 0) + (t_out or 0) + (t_reasoning or 0),
                cost=cost or None,
            )
        )
    return sessions


# ---------------------------------------------------------------- shared


def _parse_files(files: list[Path], parse_fn, cache: dict | None) -> list[Session]:
    """Parse files, reusing cached results when mtime+size are unchanged."""
    cache = cache if cache is not None else {}
    results: dict[Path, Session | None] = {}
    misses: list[Path] = []
    for path in files:
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            continue
        entry = cache.get(key)
        if entry and entry.get("mtime") == stat.st_mtime and entry.get("size") == stat.st_size:
            data = entry.get("session")
            results[path] = _session_from_dict(data) if data else None
        else:
            misses.append(path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        fresh = list(pool.map(parse_fn, misses))
    for path, session in zip(misses, fresh):
        results[path] = session
        try:
            stat = path.stat()
            cache[str(path)] = {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "session": _session_to_dict(session) if session else None,
            }
        except OSError:
            pass

    # drop cache entries for files that no longer exist or fell out of range
    current = {str(p) for p in files}
    for key in [k for k in cache if k not in current]:
        del cache[key]

    return [results[p] for p in files if results.get(p) is not None]


def _cache_file(cache_dir: Path | None) -> Path:
    cache_dir = cache_dir or Path.home() / ".cache" / "adash"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "parse-cache.json"


def _load_cache(cache_dir: Path | None) -> dict:
    try:
        with _cache_file(cache_dir).open() as fh:
            data = json.load(fh)
        if data.get("version") == CACHE_VERSION:
            return {"claude": data.get("claude", {}), "codex": data.get("codex", {})}
    except (OSError, ValueError):
        pass
    return {"claude": {}, "codex": {}}


def _save_cache(cache_dir: Path | None, cache: dict) -> None:
    try:
        with _cache_file(cache_dir).open("w") as fh:
            json.dump({"version": CACHE_VERSION, **cache}, fh)
    except OSError:
        pass


def collect_all(limit: int = DEFAULT_LIMIT, use_cache: bool = True, cache_dir: Path | None = None) -> list[Session]:
    cache = _load_cache(cache_dir) if use_cache else {"claude": {}, "codex": {}}
    sessions = (
        collect_claude(limit=limit, cache=cache["claude"])
        + collect_codex(limit=limit, cache=cache["codex"])
        + collect_opencode(limit=limit)
    )
    if use_cache:
        _save_cache(cache_dir, cache)
    sessions.sort(key=lambda s: s.last_active, reverse=True)
    return sessions
