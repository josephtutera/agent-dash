"""Shared data model for adash."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

TOOLS = ("claude", "codex", "opencode")

TOOL_COLORS = {
    "claude": "#e8865f",
    "codex": "#57b89a",
    "opencode": "#a78bfa",
}


@dataclass
class Session:
    tool: str  # "claude" | "codex" | "opencode"
    id: str
    title: str
    project_dir: str
    last_active: datetime
    n_messages: int = 0
    tokens: int = 0
    cost: float | None = None
    first_prompt: str = ""

    @property
    def project_name(self) -> str:
        return Path(self.project_dir).name if self.project_dir else "?"


def resume_invocation(session: Session) -> str:
    """The tool's own resume command, e.g. 'claude --resume abc', with no
    directory change — the caller sets the working directory separately (a Warp
    tab sets it via its `directory` field)."""
    return {
        "claude": f"claude --resume {shlex.quote(session.id)}",
        "codex": f"codex resume {shlex.quote(session.id)}",
        "opencode": f"opencode --session {shlex.quote(session.id)}",
    }[session.tool]


def resume_directory(session: Session) -> str:
    """The session's project directory, falling back to home when it's gone."""
    directory = session.project_dir
    if not directory or not Path(directory).is_dir():
        directory = str(Path.home())
    return directory


def resume_command(session: Session) -> str:
    """Shell command that resumes the session in its project directory."""
    return f"cd {shlex.quote(resume_directory(session))} && {resume_invocation(session)}"


def rel_time(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    seconds = max(0.0, (now - dt).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)}d ago"
    return dt.astimezone().strftime("%b %d")


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n) if n else "—"


# leading politeness/filler that makes prompt-derived titles unreadable.
# ordered longest-first within each family so the greediest match wins per pass.
_FILLER_PREFIXES = (
    "can you please ", "could you please ", "will you please ", "would you please ",
    "can you ", "could you ", "will you ", "would you ", "can u ", "could u ", "can ya ",
    "please help me ", "please help ", "please ", "pls ", "plz ",
    "help me to ", "help me ", "help ",
    "i want you to ", "i need you to ", "i would like you to ", "i'd like you to ",
    "id like you to ", "i would like to ", "i'd like to ", "id like to ",
    "i want to ", "i wanna ", "i need to ", "i am trying to ", "i'm trying to ",
    "im trying to ", "trying to ", "let's ", "lets ", "let me ",
    "ok so ", "okay so ", "ok ", "okay ", "so ", "hey ", "hi ", "yo ",
    "quick question ", "quick q ", "just ",
)


def clean_title(text: str, width: int = 70) -> str:
    """Turn a raw first prompt into a readable title.

    Strips filler prefixes, capitalizes, and truncates at a word boundary.
    Deterministic and offline; used only when the tool has no real title.
    """
    title = " ".join(text.split())
    for _ in range(3):  # peel stacked filler: "can you help me ..." needs two passes
        lowered = title.lower()
        prefix = next((p for p in _FILLER_PREFIXES if lowered.startswith(p)), None)
        if prefix is None:
            break
        title = title[len(prefix):]
    title = title.strip(" .")
    if title and "://" not in title.split(" ", 1)[0]:
        title = title[0].upper() + title[1:]
    if len(title) > width:
        cut = title[:width].rsplit(" ", 1)[0]
        title = cut.rstrip(",;:.") + "…"
    return title
