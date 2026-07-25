"""Read a session's conversation back as ordered turns.

`titles.conversation_excerpt` already reaches into each tool's transcript, but
only to pull three lines out for naming a session. This reads the same files as
a sequence, so the detail pane can show the conversation the way you would have
seen it in the terminal: oldest at the top, newest at the bottom, your turns and
the agent's alternating.

Two constraints shape all of it. The files are append-only logs that reach
hundreds of megabytes on a long codex session, so every reader works from a
bounded tail rather than the whole file. And this runs on the cursor, which
moves as fast as you hold the arrow key, so results are cached per session and
invalidated on mtime.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from models import Session

#: How much of the end of a transcript to parse. Big enough for a long turn to
#: survive intact, small enough that reading it is never felt.
TAIL_BYTES = 256_000
#: Turns kept per session. The pane is scrollback, not an archive.
MAX_TURNS = 20
#: A single turn longer than this is clipped; agents write essays and the point
#: of the pane is to remind you where you were, not to reread everything.
MAX_CHARS = 1400


@dataclass(frozen=True)
class Turn:
    role: str  # "user" | "agent"
    text: str


def _tail(path: Path) -> list[str]:
    """The last TAIL_BYTES of a line-delimited file, minus a possibly-truncated
    first line."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
            blob = fh.read()
    except OSError:
        return []
    lines = blob.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > TAIL_BYTES and len(lines) > 1 else lines


def _clean(text: object) -> str:
    """Collapse whitespace so a turn is one readable paragraph. Transcripts are
    full of hard-wrapped text that would otherwise wrap twice."""
    if isinstance(text, list):  # content blocks
        parts = []
        for block in text:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text") or ""))
        text = " ".join(parts)
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:MAX_CHARS]


def _is_noise(text: str) -> bool:
    """Injected scaffolding that was never something anyone said."""
    if not text:
        return True
    lowered = text.lstrip().lower()
    return lowered.startswith((
        "<command-name>", "<local-command", "<system-reminder>",
        "caveat: the messages below", "<user-prompt-submit-hook>",
    ))


def _turns_claude(path: Path) -> list[Turn]:
    turns: list[Turn] = []
    for line in _tail(path):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _clean(message.get("content"))
        if _is_noise(text):
            continue
        turns.append(Turn("user" if role == "user" else "agent", text))
    return turns


def _turns_codex(path: Path) -> list[Turn]:
    turns: list[Turn] = []
    for line in _tail(path):
        if '"payload"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = _clean(payload.get("message"))
            if not _is_noise(text):
                turns.append(Turn("user", text))
        elif obj.get("type") == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if role in ("user", "assistant"):
                text = _clean(payload.get("content"))
                if not _is_noise(text):
                    turns.append(Turn("user" if role == "user" else "agent", text))
    return turns


def _turns_gemini(path: Path) -> list[Turn]:
    turns: list[Turn] = []
    for line in _tail(path):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind not in ("user", "gemini", "model"):
            continue
        text = _clean(obj.get("content"))
        if _is_noise(text):
            continue
        turns.append(Turn("user" if kind == "user" else "agent", text))
    return turns


def _turns_opencode(db_path: Path, session_id: str) -> list[Turn]:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT m.data, p.data FROM part p "
                "JOIN message m ON p.message_id = m.id "
                "WHERE p.session_id = ? ORDER BY p.time_created DESC LIMIT ?",
                (session_id, MAX_TURNS * 4),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    turns: list[Turn] = []
    for mdata, pdata in reversed(rows):  # the query pulls the newest; restore order
        try:
            part, message = json.loads(pdata), json.loads(mdata)
        except ValueError:
            continue
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = _clean(part.get("text"))
        if _is_noise(text):
            continue
        role = message.get("role") if isinstance(message, dict) else None
        if role in ("user", "assistant"):
            turns.append(Turn("user" if role == "user" else "agent", text))
    return turns


_cache: dict[str, tuple[float, list[Turn]]] = {}


def read_turns(session: Session, limit: int = MAX_TURNS) -> list[Turn]:
    """The tail of a session's conversation, oldest first.

    Cached against the transcript's mtime, because this is called every time
    the cursor moves and a live session's file changes constantly.
    """
    if not session.source_path:
        return []
    path = Path(session.source_path)
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return []
    key = f"{session.tool}:{session.id}:{session.source_path}"
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1][-limit:]

    if session.tool == "opencode":
        turns = _turns_opencode(path, session.id)
    elif session.tool == "claude":
        turns = _turns_claude(path)
    elif session.tool == "codex":
        turns = _turns_codex(path)
    elif session.tool == "gemini":
        turns = _turns_gemini(path)
    else:
        turns = []

    # collapse consecutive turns from the same speaker: a single reply arrives
    # as several records and would otherwise read as the agent talking to itself
    merged: list[Turn] = []
    for turn in turns:
        if merged and merged[-1].role == turn.role:
            joined = f"{merged[-1].text} {turn.text}"[:MAX_CHARS]
            merged[-1] = Turn(turn.role, joined)
        else:
            merged.append(turn)

    _cache[key] = (stamp, merged)
    return merged[-limit:]
