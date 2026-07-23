"""Live activity for running agent sessions.

Reads the cheap on-disk signals each CLI exposes so the dashboard can show what
a session is doing *right now* — working vs idle, the current action, and a live
token count — without parsing whole transcripts.

Sources (all read-only, all degrade to a neutral status rather than raising):
  claude   ~/.claude/sessions/<pid>.json  (per-process status: busy/idle)
           ~/.claude/statusbar/state.json (frontmost session's label + tool)
  codex    newest ~/.codex/sessions/**/rollout-*.jsonl — bounded tail only, the
           files reach hundreds of MB, so we seek to the end instead of scanning.
  opencode ~/.local/share/opencode/opencode.db (SQLite): newest session's tokens
           plus whether its latest assistant message is still generating.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# codex payload.type -> (is the session working?, human label)
_CODEX_WORKING = {
    "task_started": "starting…",
    "reasoning": "thinking…",
    "function_call": "running tool…",
    "custom_tool_call": "running tool…",
    "patch_apply_begin": "applying edit…",
}
_CODEX_DONE = {
    "task_complete": "",
    "agent_message": "",
    "patch_apply_end": "",
}


@dataclass
class LiveStatus:
    state: str = "unknown"  # "working" | "idle" | "waiting" | "unknown"
    label: str = ""  # human action, e.g. "running command"
    tokens: int = 0  # live token count for the session (0 if unknown)
    session_id: str = ""  # transcript this process is writing ("" if unknown)


def _read_json(path: str | Path) -> dict | None:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _tail_text(path: str, n_bytes: int = 65536) -> str:
    """Return the last `n_bytes` of a (possibly huge) file, dropping a partial
    first line so JSON parsing stays clean."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > n_bytes:
                fh.seek(size - n_bytes)
                fh.readline()  # discard the partial line we landed in
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------------- claude


def claude_activity(root: str | Path | None = None) -> dict[int, LiveStatus]:
    """Map pid -> LiveStatus for every live claude session.

    Per-pid `status` gives busy/idle; the single statusbar state file enriches
    whichever session is frontmost (matched by sessionId) with its action.
    """
    base = Path(root) if root else Path.home() / ".claude"
    state = _read_json(base / "statusbar" / "state.json") or {}
    front_sid = state.get("sessionId")
    front_label = state.get("label", "") or ""
    front_state = (state.get("state") or "").lower()

    out: dict[int, LiveStatus] = {}
    for path in glob.glob(str(base / "sessions" / "*.json")):
        try:
            pid = int(Path(path).stem)
        except ValueError:
            continue
        data = _read_json(path)
        if not data:
            continue
        status = (data.get("status") or "").lower()
        sid = data.get("sessionId")
        st = LiveStatus(
            state="working" if status == "busy" else "idle" if status else "unknown",
            session_id=sid if isinstance(sid, str) else "",
        )
        if front_sid and data.get("sessionId") == front_sid:
            if front_state in ("permission", "waiting"):
                st.state = "waiting"
            elif front_state and st.state == "unknown":
                st.state = "working" if front_state in ("busy", "tool") else "idle"
            st.label = front_label
        out[pid] = st
    return out


# ---------------------------------------------------------------- codex


def codex_status_for_file(path: str | Path) -> LiveStatus:
    """LiveStatus (working/idle + tokens + last action) for one codex rollout,
    from a bounded tail of its end so it stays cheap on hundred-MB files. Used
    both for the newest-session dashboard signal and, per tab, by the in-tab
    codex title daemon."""
    tokens = 0
    last_activity = ""
    for line in _tail_text(str(path)).splitlines():
        if '"payload"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except ValueError:
            continue
        ptype = payload.get("type")
        if ptype in _CODEX_WORKING or ptype in _CODEX_DONE:
            last_activity = ptype
        info = payload.get("info") or {}
        usage = info.get("total_token_usage") or {}
        if isinstance(usage.get("total_tokens"), int):
            tokens = usage["total_tokens"]
    if last_activity in _CODEX_WORKING:
        return LiveStatus(state="working", label=_CODEX_WORKING[last_activity], tokens=tokens)
    return LiveStatus(state="idle" if last_activity else "unknown", tokens=tokens)


def codex_activity(root: str | Path | None = None) -> LiveStatus | None:
    """LiveStatus for the newest codex rollout (tokens + last action), or None."""
    base = Path(root) if root else Path.home() / ".codex" / "sessions"
    files = sorted(
        glob.glob(str(base / "**" / "rollout-*.jsonl"), recursive=True),
        key=os.path.getmtime,
        reverse=True,
    )
    if not files:
        return None
    return codex_status_for_file(files[0])


# ---------------------------------------------------------------- opencode


def opencode_activity(db_path: str | Path | None = None) -> LiveStatus | None:
    """LiveStatus for the newest active opencode session, or None."""
    path = str(db_path or Path.home() / ".local/share/opencode/opencode.db")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT id, tokens_input, tokens_output, tokens_reasoning FROM session "
            "WHERE time_archived IS NULL ORDER BY time_updated DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        sid, ti, to, tr = row
        tokens = (ti or 0) + (to or 0) + (tr or 0)
        state = "idle"
        msg = con.execute(
            "SELECT data FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if msg:
            try:
                data = json.loads(msg[0])
                if data.get("role") == "assistant" and not (data.get("time") or {}).get("completed"):
                    state = "working"
            except (ValueError, TypeError):
                pass
        return LiveStatus(state=state, tokens=tokens)
    except sqlite3.Error:
        return None
    finally:
        con.close()


# ---------------------------------------------------------------- join


def enrich(agents, claude_root=None, codex_root=None, opencode_db=None):
    """Attach live status to running agents in place, and return them.

    claude is matched precisely by pid; codex/opencode expose a single global
    activity signal, applied to that tool's agents (typically just one).
    """
    claude = claude_activity(claude_root)
    codex = ...  # lazily fetched only if a codex agent is present
    opencode = ...
    for agent in agents:
        if agent.tool == "claude":
            status = claude.get(agent.pid)
        elif agent.tool == "codex":
            if codex is ...:
                codex = codex_activity(codex_root)
            status = codex
        elif agent.tool == "opencode":
            if opencode is ...:
                opencode = opencode_activity(opencode_db)
            status = opencode
        else:
            status = None
        if status:
            agent.state = status.state
            agent.label = status.label
            agent.tokens = status.tokens
            agent.session_id = status.session_id
    return agents
