"""In-tab title daemon: give an agent-dash-launched codex Warp tab a live,
conversation-aware title, the way Claude Code titles its own tab.

Why a daemon at all: codex can only assemble its tab title from a fixed menu of
items (status, project, git-branch, …) — none of them a task description, and it
never auto-names a session — so a codex tab can't title itself after the work.
Warp only honors title writes from a tab's *own* processes, so the dashboard
can't rename a running codex tab from outside either (see the warp-osc-tab-titles
note). So agent-dash launches this daemon *inside* the codex tab, backgrounded
next to codex (which runs with tui.terminal_title=[] so it won't fight). The
daemon works out which codex session the tab is running, then rewrites the tab
title from agent-dash's own title cache every poll — with a braille working
spinner while codex is active, exactly like the Claude tab beside it.

It reuses the dashboard's machinery end to end: collectors to find the session,
titles.py (shared TitleStore cache) for the title, activity.py for working/idle.
So a codex tab and the dashboard always show the same title, and neither pays to
generate one the other already made.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from activity import codex_status_for_file
from collectors import collect_codex
from models import SPINNER_FRAMES, Session, osc_title_sequence
from titles import TitleStore, cached_or_fallback_title, generate_title, needs_title

SPIN_SECONDS = 0.15  # spinner animation cadence (title recompose + OSC write)
RESOLVE_SECONDS = 2.0  # how often to look for this tab's session until found
RETITLE_SECONDS = 30.0  # how often to re-read a found session (growth → new title)
STATE_SECONDS = 2.0  # how often to re-check working/idle (a cheap file tail)
RESOLVE_LIMIT = 60  # codex sessions to scan when resolving (newest first)
RESOLVE_GRACE = 5.0  # a fresh session started this many secs before us still counts


def _same_dir(a: str, b: str) -> bool:
    """True when two paths point at the same directory, resolving ~ and symlinks
    so codex's stored cwd matches the tab's launch directory."""
    if not a:
        return False
    try:
        return os.path.realpath(os.path.expanduser(a)) == b
    except OSError:
        return False


def resolve_codex_session(
    cwd: str,
    session_id: str = "",
    since: float = 0.0,
    *,
    root: Path | None = None,
    limit: int = RESOLVE_LIMIT,
    grace: float = RESOLVE_GRACE,
    cache: dict | None = None,
) -> Session | None:
    """The codex session this tab is running.

    On resume the id is known, so match it exactly. On a fresh launch it isn't,
    so take the newest codex session whose directory matches and which started
    around when we did (``since - grace``) — that filters out an older session
    left over in the same repo.
    """
    sessions = collect_codex(root=root, limit=limit, cache=cache if cache is not None else {})
    if session_id:
        return next((s for s in sessions if s.id == session_id), None)
    target = os.path.realpath(os.path.expanduser(cwd))
    cutoff = since - grace
    for session in sessions:  # collect_codex returns newest-active first
        if session.last_active.timestamp() >= cutoff and _same_dir(session.project_dir, target):
            return session
    return None


def compose_tab_title(base: str, working: bool, frame_char: str) -> str:
    """The exact string written to the tab: a spinner prefix while codex works,
    the bare title when it's idle — matching Claude's tab."""
    return f"{frame_char} {base}" if working and base else base


def _dir_label(cwd: str) -> str:
    """Placeholder shown before the session is found, matching the tab-config
    name so nothing flickers on launch."""
    return f"codex · {Path(cwd).name or cwd}"


def _thread_spawn(fn) -> None:
    import threading

    threading.Thread(target=fn, daemon=True).start()


class CodexTabTitler:
    """The daemon's state, split from its run loop so the decision logic is unit
    testable. ``tick(now)`` returns the title to display at monotonic time
    ``now``; the loop just writes it and sleeps."""

    def __init__(
        self,
        cwd: str,
        session_id: str = "",
        *,
        store: TitleStore | None = None,
        codex_root: Path | None = None,
        since: float = 0.0,
        resolve_every: float = RESOLVE_SECONDS,
        retitle_every: float = RETITLE_SECONDS,
        state_every: float = STATE_SECONDS,
        generate=generate_title,
        spawn=_thread_spawn,
        status_for_file=codex_status_for_file,
    ):
        self.cwd = cwd
        self.session_id = session_id
        self.store = store or TitleStore()
        self.codex_root = codex_root
        self.since = since
        self.resolve_every = resolve_every
        self.retitle_every = retitle_every
        self.state_every = state_every
        self._generate = generate
        self._spawn = spawn
        self._status_for_file = status_for_file

        self.session: Session | None = None
        self.base = ""
        self.working = False
        self.frame = 0
        self._parse_cache: dict = {}  # in-memory only; never written to disk
        self._next_resolve = 0.0
        self._next_retitle = 0.0
        self._next_state = 0.0
        self._gen_inflight = False
        self._pending = None  # (id, title, n_messages) from the generator thread

    def tick(self, now: float) -> str:
        self._maybe_resolve(now)
        self._adopt_generation()
        self._maybe_state(now)
        if self.working:
            self.frame += 1
        frame_char = SPINNER_FRAMES[self.frame % len(SPINNER_FRAMES)]
        base = self.base if self.base and self.base != "(untitled)" else _dir_label(self.cwd)
        return compose_tab_title(base, self.working, frame_char)

    def _maybe_resolve(self, now: float) -> None:
        due = self._next_resolve if self.session is None else self._next_retitle
        if now < due:
            return
        session = resolve_codex_session(
            self.cwd, self.session_id, self.since,
            root=self.codex_root, cache=self._parse_cache,
        )
        if session is not None:
            self.session = session
            self._refresh_base(session)
            self._next_retitle = now + self.retitle_every
        self._next_resolve = now + self.resolve_every

    def _refresh_base(self, session: Session) -> None:
        self.base = cached_or_fallback_title(session, self.store)
        if not self._gen_inflight and needs_title(session, self.store):
            self._gen_inflight = True
            self._spawn(lambda: self._run_generation(session))

    def _run_generation(self, session: Session) -> None:
        # Runs off the main loop so the spinner keeps animating during the
        # ~15-25s codex-exec title call. Only stashes the result; the main loop
        # persists it (below) so the cache is touched from one thread.
        title = self._generate(session)
        self._pending = (session.id, title, session.n_messages) if title else ("", "", 0)

    def _adopt_generation(self) -> None:
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        self._gen_inflight = False
        sid, title, n_messages = pending
        if not title:
            return
        self.store.put(sid, title, n_messages, "codex")
        if self.session is not None and self.session.id == sid:
            self.base = title

    def _maybe_state(self, now: float) -> None:
        if self.session is None or now < self._next_state:
            return
        status = self._status_for_file(self.session.source_path)
        self.working = bool(status and status.state == "working")
        self._next_state = now + self.state_every


def _open_tty():
    """The tab's terminal, written to directly so the daemon's own stdout can be
    sent to /dev/null without losing the title. None if there's no controlling
    terminal (then there's nothing to title)."""
    try:
        return open("/dev/tty", "w")
    except OSError:
        return None


def _write_title(tty, text: str) -> bool:
    """Write one OSC title to the tab. False if the terminal has gone away, which
    the loop treats as 'tab closed, stop'."""
    if tty is None:
        return False
    try:
        tty.write(osc_title_sequence(text))
        tty.flush()
        return True
    except OSError:
        return False


def _parent_alive() -> bool:
    """False once our parent shell is gone (tab closed → reparented to launchd),
    a backstop in case the launch command's explicit kill is missed."""
    return os.getppid() != 1


def run_codex_titles(cwd: str, session_id: str = "", *, codex_root: Path | None = None) -> None:
    """Entry point for `main.py --codex-titles`. Titles the current tab until
    codex exits, the tab closes, or we're signalled, then hands the title back to
    the shell."""
    if os.environ.get("TERM") == "dumb":
        return
    tty = _open_tty()
    if tty is None:
        return
    titler = CodexTabTitler(cwd, session_id, since=time.time(), codex_root=codex_root)

    stop = {"v": False}

    def _stop(_signum, _frame):
        stop["v"] = True

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    last = None
    try:
        while not stop["v"] and _parent_alive():
            text = titler.tick(time.monotonic())
            if text != last:
                if not _write_title(tty, text):
                    break
                last = text
            time.sleep(SPIN_SECONDS)
    finally:
        _write_title(tty, "")  # clear the override so the shell's display returns
        try:
            tty.close()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="adash-codex-titles")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--session", default="")
    args = parser.parse_args(argv)
    run_codex_titles(args.cwd, args.session)


if __name__ == "__main__":
    main()
