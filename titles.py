"""Generate one canonical, conversation-aware title per session.

agent-dash owns the title now: rather than surfacing whatever each tool stored
(Claude's aiTitle, Codex's raw prompt, OpenCode's DB title), it reads a few
turns of the actual conversation and asks the Codex CLI for a short summary, in
one consistent voice across all three tools.

The heavy work is deliberately isolated here so the hot parse path in
collectors.py stays metadata-only:

- Titles are cached by session id (see TitleStore), so a generated title
  survives the transcript being appended to — unlike collectors' parse cache,
  which is keyed by file mtime and thrown away on every write.
- The conversation excerpt is read on demand, only for the handful of sessions
  about to be titled, by re-opening each session's source_path.
- Generation shells out to `codex exec` with the small/cheap model at minimal
  reasoning effort, so it barely touches the user's usage.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from collectors import _CODEX_NEEDLES, _user_text
from models import Session, clean_title

# small/cheap model (Luna replaced GPT-5.4 Mini); 'none' reasoning (Luna's
# lowest tier — it rejects 'minimal') keeps the spend per title near zero.
CODEX_MODEL = "gpt-5.6-luna"
CODEX_REASONING_EFFORT = "none"
CODEX_TIMEOUT_SECONDS = 25
EXCERPT_BUDGET = 1500  # max chars of conversation fed to the titler
TITLE_MAX_CHARS = 60

TITLE_PROMPT = (
    "You write a short title for an AI coding session, summarizing what it is "
    "about from the conversation excerpt below.\n"
    "Rules: at most 6 words; Title Case; no surrounding quotes; no trailing "
    "punctuation; no prefix like 'Title:'. Output only the title.\n\n"
    "Conversation excerpt:\n"
)


# ---------------------------------------------------------------- title store

TITLE_CACHE_VERSION = 1


def _title_cache_file(cache_dir: Path | None) -> Path:
    cache_dir = cache_dir or Path.home() / ".cache" / "adash"
    return cache_dir / "titles.json"


class TitleStore:
    """Generated titles persisted to ~/.cache/adash/titles.json, keyed by
    session id so they outlive transcript edits. `n_messages` records how long
    the conversation was when the title was made, so titles re-generate as a
    session grows (see needs_title)."""

    def __init__(self, cache_dir: Path | None = None):
        self.path = _title_cache_file(cache_dir)
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if data.get("version") == TITLE_CACHE_VERSION and isinstance(data.get("titles"), dict):
            return data["titles"]
        return {}

    def get(self, session_id: str) -> dict | None:
        return self._data.get(session_id)

    def put(self, session_id: str, title: str, n_messages: int, tool: str) -> None:
        self._data[session_id] = {"title": title, "n_messages": n_messages, "tool": tool}
        self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"version": TITLE_CACHE_VERSION, "titles": self._data})
            )
        except OSError:
            pass


def needs_title(session: Session, store: TitleStore) -> bool:
    """True when a session has no cached title yet, or its conversation has grown
    materially since the cached one was made. The doubling + absolute-growth gate
    means short sessions title exactly once and long ones refine a few times,
    rather than re-generating on every poll."""
    entry = store.get(session.id)
    if entry is None:
        return True
    cached_n = int(entry.get("n_messages") or 0)
    n = session.n_messages
    return n >= 2 * cached_n and n - cached_n >= 8


def apply_titles(sessions: list[Session], store: TitleStore) -> list[Session]:
    """Overlay cached generated titles onto sessions (in place) and return the
    ones eligible for (re)generation. A session with a stale-but-cached title
    keeps showing it while a fresher one generates, so the row never reverts to
    the raw first prompt once it has been titled."""
    eligible: list[Session] = []
    for session in sessions:
        entry = store.get(session.id)
        if entry and entry.get("title"):
            session.title = entry["title"]
        if needs_title(session, store):
            eligible.append(session)
    return eligible


# ---------------------------------------------------------------- excerpts


def _assistant_text(content) -> str | None:
    """Text a model actually said, skipping thinking/reasoning and tool-use
    blocks. Handles Claude ('text') and Codex ('output_text') block shapes."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") in ("text", "output_text")
        ]
        text = "\n".join(part for part in parts if part)
    else:
        return None
    text = " ".join(text.split())
    return text or None


def _compose_excerpt(first_user: str, first_asst: str, last_user: str) -> str:
    """Assemble the labeled excerpt fed to the titler, within EXCERPT_BUDGET."""
    sections: list[tuple[str, str]] = []
    if first_user:
        sections.append(("User", first_user[:700]))
    if first_asst:
        sections.append(("Assistant", first_asst[:600]))
    if last_user and last_user != first_user:
        sections.append(("Latest user message", last_user[:400]))
    text = "\n\n".join(f"{label}: {body}" for label, body in sections)
    return text[:EXCERPT_BUDGET]


def _excerpt_claude(path: Path) -> str:
    first_user = first_asst = last_user = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"type"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                kind = obj.get("type")
                if kind == "user":
                    text = _user_text(message.get("content"))
                    if text:
                        first_user = first_user or text
                        last_user = text
                elif kind == "assistant" and not first_asst:
                    first_asst = _assistant_text(message.get("content")) or ""
    except OSError:
        return ""
    return _compose_excerpt(first_user, first_asst, last_user)


def _excerpt_codex(path: Path) -> str:
    first_user = first_asst = last_user = ""
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
                if kind == "event_msg" and payload.get("type") == "user_message":
                    text = _user_text(payload.get("message"))
                    if text:
                        first_user = first_user or text
                        last_user = text
                elif kind == "response_item" and payload.get("type") == "message":
                    role = payload.get("role")
                    if role == "user":
                        text = _user_text(payload.get("content"))
                        if text:
                            first_user = first_user or text
                            last_user = text
                    elif role == "assistant" and not first_asst:
                        first_asst = _assistant_text(payload.get("content")) or ""
    except OSError:
        return ""
    return _compose_excerpt(first_user, first_asst, last_user)


def _excerpt_gemini(path: Path) -> str:
    """Gemini writes one JSON object per line: a metadata header, then message
    objects tagged type "user" | "gemini" with string content."""
    first_user = first_asst = last_user = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                kind = obj.get("type")
                if kind == "user":
                    text = _user_text(obj.get("content"))
                    if text:
                        first_user = first_user or text
                        last_user = text
                elif kind in ("gemini", "model") and not first_asst:
                    first_asst = _assistant_text(obj.get("content")) or ""
    except OSError:
        return ""
    return _compose_excerpt(first_user, first_asst, last_user)


def _excerpt_opencode(db_path: Path, session_id: str) -> str:
    """OpenCode stores conversation text in the `part` table (type 'text'),
    joined to `message` for the speaker's role."""
    first_user = first_asst = last_user = ""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT m.data, p.data FROM part p "
                "JOIN message m ON p.message_id = m.id "
                "WHERE p.session_id = ? ORDER BY p.time_created LIMIT 200",
                (session_id,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    for mdata, pdata in rows:
        try:
            part = json.loads(pdata)
            message = json.loads(mdata)
        except ValueError:
            continue
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = " ".join((part.get("text") or "").split())
        if not text:
            continue
        role = message.get("role") if isinstance(message, dict) else None
        if role == "user":
            first_user = first_user or text
            last_user = text
        elif role == "assistant" and not first_asst:
            first_asst = text
    return _compose_excerpt(first_user, first_asst, last_user)


def conversation_excerpt(session: Session) -> str:
    """A small labeled excerpt of the session's conversation, or "" if the
    source is missing/unreadable."""
    if not session.source_path:
        return ""
    path = Path(session.source_path)
    if session.tool == "opencode":
        return _excerpt_opencode(path, session.id)
    if not path.is_file():
        return ""
    if session.tool == "claude":
        return _excerpt_claude(path)
    if session.tool == "codex":
        return _excerpt_codex(path)
    if session.tool == "gemini":
        return _excerpt_gemini(path)
    return ""


# ---------------------------------------------------------------- generation


def sanitize_title(raw: str, width: int = TITLE_MAX_CHARS) -> str:
    """Clean a model's reply into a tab-title: last non-empty line, no label
    prefix, no surrounding quotes, no trailing punctuation, clamped to width."""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    title = lines[-1] if lines else ""
    title = re.sub(r"^(session\s+)?title\s*[:\-]\s*", "", title, flags=re.IGNORECASE)
    title = title.strip().strip("\"'“”‘’").strip()
    title = " ".join(title.split()).rstrip(".!,;:")
    if len(title) > width:
        title = title[:width].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return title


def _run_codex_titler(prompt: str, timeout: int = CODEX_TIMEOUT_SECONDS) -> str | None:
    """Ask the Codex CLI for a title. Returns the raw last message, or None on
    any failure (non-zero exit, timeout, codex not installed). --ephemeral keeps
    these calls from creating rollout files that would show up in agent-dash's
    own history; --ignore-user-config bypasses the user's high-reasoning default
    and notify hooks while auth still resolves from CODEX_HOME."""
    fd, out_path = tempfile.mkstemp(prefix="adash-title-", suffix=".txt")
    os.close(fd)
    try:
        proc = subprocess.run(
            [
                "codex", "exec",
                "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                "-s", "read-only",
                "-m", CODEX_MODEL,
                "-c", f"model_reasoning_effort={CODEX_REASONING_EFFORT}",
                "-o", out_path,
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            # no stdin: when adash runs inside a TUI, an inherited terminal stdin
            # makes codex block/misbehave; a neutral cwd keeps it from loading the
            # host repo's AGENTS.md into the titling turn (slower + off-topic).
            stdin=subprocess.DEVNULL,
            cwd=tempfile.gettempdir(),
        )
        if proc.returncode != 0:
            return None
        try:
            raw = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        return raw or None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _fallback_title(session: Session) -> str:
    """Deterministic title when generation is unavailable — the same logic the
    collectors use, so a failed generation is no worse than no generation."""
    if session.first_prompt:
        return clean_title(session.first_prompt)
    return session.title or "(untitled)"


def cached_or_fallback_title(session: Session, store: TitleStore) -> str:
    """The title to show right now: the cached generated one if there is one,
    else the collector's title (codex thread name / cleaned first prompt). Mirrors
    what apply_titles overlays on the dashboard, so a codex tab and the dashboard
    always agree on the same string."""
    entry = store.get(session.id)
    if entry and entry.get("title"):
        return entry["title"]
    return session.title or _fallback_title(session)


def generate_title(session: Session) -> str:
    """The canonical title for a session. Always returns a string: the generated
    title on success, else a deterministic fallback (so a transient codex failure
    is cached with the current message count and simply retried once the
    conversation grows, rather than hammering codex every poll)."""
    excerpt = conversation_excerpt(session)
    if excerpt:
        raw = _run_codex_titler(TITLE_PROMPT + excerpt)
        if raw:
            title = sanitize_title(raw)
            if title:
                return title
    return _fallback_title(session)
