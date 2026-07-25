"""Textual TUI for adash: subscription usage + active agents + session history."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import DEVNULL, Popen

from rich.markup import escape
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Input, Static

import bridge
import transcript
from activity import enrich
from agents import RunningAgent, running_agents
from collectors import collect_all
from models import (
    SPINNER_FRAMES as _SPINNER,
    TOOL_COLORS,
    TOOLS,
    Session,
    fmt_tokens,
    rel_time,
    resume_directory,
    resume_invocation,
)
from titles import TitleStore, apply_titles, generate_title
from usage import ToolUsage, UsageWindow, claude_profiles, collect_usage

# aligned subscriptions grid: fixed-width columns so every 5h bar lines up in
# one column and every 7d bar in another, under a single header.
USAGE_TOOL_W = 9  # colored tool name column (widest is "opencode" = 8)
USAGE_PLAN_W = 14  # dim plan column, e.g. "Team·default", "pay-as-you-go"
USAGE_BAR_W = 8  # mini utilization bar inside each window cell
USAGE_RESET_W = 6  # compact reset time, e.g. "10:59a"
USAGE_PCT_W = 4  # right-aligned percent, e.g. " 16%", "100%"
# total visible width of one window cell (bar + space + pct + space + reset)
USAGE_CELL_W = USAGE_BAR_W + 1 + USAGE_PCT_W + 1 + USAGE_RESET_W
USAGE_GAP = "   "  # between the 5h and 7d columns
# the launcher can start any agent CLI, or just a plain shell ("terminal")
LAUNCH_TOOLS = ("claude", "codex", "opencode", "gemini", "terminal")
# braille spinner frames for the "working" indicator, like the CLIs themselves
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ACTIVE_POLL_SECONDS = 2.0  # live activity refresh (cheap: file reads + one ps)
# History refresh. The session list is read from local files (mtime-cached in
# collectors, so re-scans are cheap), but it used to load only once at startup,
# which meant a session you just started, or the one you're sitting in, never
# showed up until you hit r. Poll it on a gentle loop so history stays current.
HISTORY_POLL_SECONDS = 10.0
# Title generation runs on the history poll: at most this many sessions are
# titled per pass, and only when no batch is already in flight, so a big backlog
# drips in over several passes instead of spawning a flood of codex processes.
TITLE_BATCH = 4
# Usage refresh. Claude hits a rate-limited API once per account here, and every
# running Claude Code session polls the same endpoint against the same quota, so
# this stays slow on purpose: the 5h/7d bars move far too gradually to be worth
# a tighter loop. usage.py caches on top of this and backs off on a 429.
USAGE_POLL_SECONDS = 180.0
# Boot "self-test" sweep: on the first usage load every gauge rises to a
# full-scale peg, then eases down to its true reading, the way an instrument
# panel runs its needle sweep at ignition. _boot goes 0 -> 1 over these seconds.
BOOT_SWEEP_SECONDS = 0.85
_BOOT_PEAK = 0.55  # fraction of the sweep spent rising to the peg before settling
# Below this the two panes stop being readable, so the detail pane steps aside
# rather than the app telling you your terminal is the wrong shape.
NARROW_COLUMNS = 100
CONFIG_PATH = Path.home() / ".config" / "agent-dash" / "config.json"


THEME_PREFS = ("auto", "light", "dark")  # what T cycles through


def system_theme() -> str:
    """Whether macOS is currently in dark mode.

    `defaults read -g AppleInterfaceStyle` prints "Dark" in dark mode and exits
    non-zero in light mode, because the key is simply absent there. That quirk
    is the whole detection: a failed read means light, not an error.
    """
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return bridge.DEFAULT_THEME  # not macOS, or defaults is unavailable
    return "dark" if result.returncode == 0 and "dark" in result.stdout.lower() else "light"


def _load_theme_pref() -> str:
    """A theme toggle you have to re-apply on every launch is a party trick, so
    the choice is remembered. A missing or unreadable config is not worth a
    warning: follow the system and carry on."""
    try:
        name = json.loads(CONFIG_PATH.read_text()).get("theme")
    except Exception:
        return "auto"
    return name if name in THEME_PREFS else "auto"


def _save_theme_pref(name: str) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({"theme": name}))
    except OSError:
        pass  # a read-only home should not stop you flipping the lights


def _truncate(text: str, width: int) -> str:
    return text[: width - 1] + "…" if len(text) > width else text


def _usage_color(pct: float) -> str:
    # instrument palette: calm blue until it runs warm, then amber, then red
    return "#7aa2f7" if pct < 60 else "#ffb454" if pct < 85 else "#ff5c57"


def _fmt_reset_compact(dt: datetime | None) -> str:
    """Reset time squeezed to fit a usage column: '10:59a', '5:30p', 'Jul24'."""
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    local = dt.astimezone()
    if dt - now < timedelta(hours=24):
        return local.strftime("%-I:%M%p").lower().replace("am", "a").replace("pm", "p")
    return local.strftime("%b%d")


def _ease_out(t: float) -> float:
    """Cubic ease-out (fast start, gentle stop) — a needle easing into place."""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _sweep_pct(true_pct: float | None, progress: float) -> float | None:
    """Reading a gauge shows at boot frame `progress` (0..1): it rises from 0 to
    a full-scale peg, then eases down to its true value — an instrument self-test
    sweep. A window a tool doesn't report (None) stays None the whole way."""
    if true_pct is None:
        return None
    if progress >= 1.0:
        return true_pct
    if progress <= _BOOT_PEAK:  # rising to the peg
        return 100.0 * _ease_out(progress / _BOOT_PEAK)
    settle = _ease_out((progress - _BOOT_PEAK) / (1.0 - _BOOT_PEAK))  # peg -> true
    return 100.0 + (true_pct - 100.0) * settle


def _usage_bar(pct: float | None) -> str:
    """Mini bar of exactly USAGE_BAR_W visible columns for the aligned grid."""
    if pct is None:
        return f"[#2a3348]{'░' * USAGE_BAR_W}[/]"
    filled = round(USAGE_BAR_W * min(pct, 100.0) / 100.0)
    return f"[{_usage_color(pct)}]{'█' * filled}[/][#1b2233]{'░' * (USAGE_BAR_W - filled)}[/]"


def _window_cell(win: "UsageWindow | None", progress: float = 1.0) -> str:
    """A fixed-width (USAGE_CELL_W) cell: mini-bar + percent + compact reset.

    A missing window (a tool that doesn't report this limit) renders as an empty
    bar with a dim dash, so the column still lines up with its neighbors.
    `progress` < 1.0 sweeps the reading during the boot self-test; the bar,
    percent, and color all track the swept value so the whole cell animates.
    """
    pct = win.pct if win else None
    if progress < 1.0:
        pct = _sweep_pct(pct, progress)
    bar = _usage_bar(pct)
    if pct is None:
        pct_markup = f"[#55647f]{'—'.rjust(USAGE_PCT_W)}[/]"
    else:
        pct_markup = f"[{_usage_color(pct)}]{f'{pct:.0f}%'.rjust(USAGE_PCT_W)}[/]"
    reset = _fmt_reset_compact(win.resets_at) if win else ""
    return f"{bar} {pct_markup} [dim]{reset.ljust(USAGE_RESET_W)}[/]"


def _pad_visible(markup: str, visible_len: int, width: int) -> str:
    """Right-pad a markup string to `width` visible columns."""
    return markup + " " * max(0, width - visible_len)


_TAB_COLORS = {"claude": "magenta", "codex": "green", "opencode": "blue", "gemini": "cyan", "terminal": "blue"}
# None means "open a plain shell in the directory" — no agent command is run
_TAB_COMMANDS = {"claude": "claude", "codex": "codex", "opencode": "opencode", "gemini": "gemini", "terminal": None}

# codex can only build its tab title from a fixed menu of items (status, project,
# git-branch, …); none of them is a task description, and it never auto-names a
# session, so a codex tab can't title itself after the work the way Claude Code
# does. Warp also only honors title writes from a tab's own processes, so the
# dashboard can't rename a running codex tab from outside. To reach parity we
# launch a small in-tab daemon (main.py --codex-titles) alongside codex that
# rewrites the tab title from agent-dash's own generated title, and we silence
# codex's own title (terminal_title=[]) so the two don't fight.
_CODEX_SUPPRESS_TITLE = "-c 'tui.terminal_title=[]'"


def _codex_resume_id(command: str) -> str:
    """The session id from a `codex resume <id>` command, else "" for a fresh
    launch. Passed to the title daemon so it tracks the resumed session exactly
    instead of guessing from the directory."""
    parts = shlex.split(command)
    if len(parts) >= 3 and parts[0] == "codex" and parts[1] == "resume":
        return parts[2]
    return ""


def _codex_title_daemon(cwd: str, session_id: str) -> str:
    """The `python main.py --codex-titles …` invocation that titles this tab.
    Uses the same interpreter and repo as the running dashboard so it shares the
    title cache and needs no separate install."""
    python = shlex.quote(sys.executable)
    script = shlex.quote(str(Path(__file__).resolve().parent / "main.py"))
    cmd = f"{python} {script} --codex-titles --cwd {shlex.quote(cwd)}"
    if session_id:
        cmd += f" --session {shlex.quote(session_id)}"
    return cmd


def _codex_launch_command(command: str, cwd: str, env_prefix: str) -> str:
    """Wrap a codex launch so the tab gets a Claude-style live title: start the
    title daemon in the background, run codex in the foreground with its own
    title silenced, and kill the daemon when codex exits."""
    head, _, tail = command.partition(" ")  # head="codex", tail="resume <id>"|""
    codex = f"{head} {_CODEX_SUPPRESS_TITLE}" + (f" {tail}" if tail else "")
    daemon = _codex_title_daemon(cwd, _codex_resume_id(command))
    return (
        f"{daemon} >/dev/null 2>&1 & _adtw=$!; "
        f"{env_prefix}{codex}; kill $_adtw 2>/dev/null"
    )


def _toml_basic(value: str) -> str:
    """Escape a string for a TOML basic (double-quoted) string, so a command that
    itself contains quotes (like the codex `-c` array) survives the round-trip."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _warp_configs_dir() -> Path:
    return Path.home() / ".warp" / "tab_configs"


ACCESSIBILITY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
_READ_TAB_TITLE = 'tell application "System Events" to tell process "Warp" to get title of front window'
_NEXT_TAB = ('tell application "System Events" to tell process "Warp" to '
             'keystroke "]" using {command down, shift down}')


def _osa(script: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    except Exception as exc:  # osascript missing/timed out: treat as unavailable
        return False, str(exc)
    return proc.returncode == 0, (proc.stdout or proc.stderr).strip()


def _focus_warp_tab(hints: list[str], max_tabs: int = 16) -> str:
    """Bring Warp to front and select the tab whose title matches a hint.

    `hints` are matched case-insensitively as substrings of the window title
    (Warp's window title tracks the active tab). Warp's URI scheme can only
    open NEW tabs, so finding an existing one means cycling with the next-tab
    keystroke, which needs Accessibility permission. Returns "focused",
    "activated" (Warp is front-most but no tab matched), or "denied".
    """
    ok, start_title = _osa(_READ_TAB_TITLE)
    if not ok:
        return "denied"
    _osa('tell application "Warp" to activate')
    lowered = [h.lower() for h in hints if h]
    for i in range(max_tabs):
        ok, title = _osa(_READ_TAB_TITLE)
        if ok and any(h in title.lower() for h in lowered):
            return "focused"
        # once we cycle back to the tab we started on, we've seen them all;
        # stop instead of rotating through the whole set again (the old code
        # kept pressing a fixed 16 times, spinning past every tab 3+ times).
        if i > 0 and ok and title == start_title:
            return "activated"
        _osa(_NEXT_TAB)
        time.sleep(0.12)  # let Warp switch before re-reading the title
    return "activated"


def _is_worktree(path: str) -> bool:
    """True for throwaway agent worktrees (codex `~/.codex/worktrees/...`, Claude
    Code `<repo>/.claude/worktrees/...`). These clutter the new-session picker
    since you never start a fresh session in one."""
    return "/worktrees/" in path


def _dir_slug(cwd: str) -> str:
    """A filename-safe slug from a directory basename, for per-dir tab configs."""
    name = Path(cwd).name or "root"
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "root"


def _write_tab_config(tool: str, cwd: str, configs_dir: Path, suffix: str = "", env: dict | None = None, command: str | None = None) -> str:
    """Generate (or refresh) a tab config that opens `tool` in `cwd`; return its stem.

    `suffix` distinguishes tabs launched in different directories (e.g. opening
    claude in several repos at once). `env` prepends VAR=value assignments to the
    command (used to point claude at a non-default CLAUDE_CONFIG_DIR account).
    `command` overrides the default tool command — used to resume a specific
    session (e.g. `claude --resume <id>`) rather than start a fresh one. When the
    tool has no command (terminal) and no override, the tab opens a bare shell.
    """
    stem = f"agentdash-{tool}" + (f"-{suffix}" if suffix else "")
    label = "terminal" if tool == "terminal" else tool
    lines = [
        f'name = "{label} · {Path(cwd).name or cwd}"',
        f'color = "{_TAB_COLORS[tool]}"',
        "",
        "[[panes]]",
        'id = "main"',
        'type = "terminal"',
        f'directory = "{cwd}"',
    ]
    command = command if command is not None else _TAB_COMMANDS[tool]
    if command:
        prefix = "".join(f"{k}={v} " for k, v in (env or {}).items())
        if tool == "codex":
            full = _codex_launch_command(command, cwd, prefix)
        else:
            full = prefix + command
        lines.append(f'commands = ["{_toml_basic(full)}"]')
    content = "\n".join(lines) + "\n"
    target = configs_dir / f"{stem}.toml"
    if not target.exists() or target.read_text() != content:
        configs_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return stem


@dataclass
class Entry:
    """One row of the single list.

    The old screen had four zones (launcher, directory picker, active-now,
    history) chained together by arrow keys, which meant four cursors to keep
    in sync and a mental model you had to be taught. There is now one list, so
    there is one cursor, and `key` is what keeps it steady: the active poll
    rebuilds this list every two seconds, and re-finding the selection by
    identity rather than by index is the difference between the cursor staying
    where you left it and it sliding out from under your hands.
    """

    kind: str  # "agent" | "session"
    key: str
    agent: RunningAgent | None = None
    session: Session | None = None


class _ListPane(Static):
    """The left pane, where clicking a row selects it the way arrowing does.

    Click lands here rather than on the app: under Textual 8 mouse events go to
    the widget under the cursor and never reach app-level handlers, unlike keys.
    """

    def on_click(self, event: events.Click) -> None:
        app = self.app
        if not isinstance(app, AdashApp):
            return
        index = app._entry_at_line(event.y)
        if index is None:
            return
        already_selected = index == app.cursor
        app._cursor_moved = True
        app.cursor = index
        app.mode = "browse"
        app._render_all()
        if already_selected:
            app.action_primary()  # second click on the same row commits it


class _OverlayScreen(ModalScreen):
    """A dismiss-on-any-key panel. Used for `?` (the full keymap) and `u` (the
    usage grid) — the two things worth having on demand and not worth spending
    permanent screen space on."""

    def __init__(self, body: str, title: str) -> None:
        super().__init__()
        self._body = body
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="overlay"):
            yield Static(self._title, id="overlay-title")
            yield Static(self._body, id="overlay-body")
            yield Static("any key closes", id="overlay-hint")

    def on_key(self, event) -> None:
        event.stop()
        self.dismiss()

    def on_click(self) -> None:
        self.dismiss()


class AdashApp(App):
    TITLE = "Agent Dash"

    # Colours come from bridge.THEMES via get_css_variables, so a theme switch
    # is a variable swap and a refresh_css() rather than a second stylesheet.
    CSS = """
    Screen { background: $ad-ground; color: $ad-text; }
    #header { height: 3; padding: 1 3 0 3; border-bottom: solid $ad-rule; }
    #body { height: 1fr; }
    #list {
        width: 54;
        border-right: solid $ad-rule;
        padding: 1 2 1 1;
        scrollbar-size-vertical: 1;
    }
    #listbody { height: auto; }
    #detail { width: 1fr; padding: 1 3 0 3; }
    #detailscroll { height: 1fr; scrollbar-size-vertical: 1; }
    #detailbody { height: auto; }
    #action { height: auto; padding: 1 0 1 0; }
    #footer { height: 2; padding: 0 3; border-top: solid $ad-rule; }
    /* Under ~100 columns two panes stop being readable, so the detail pane
       steps aside and the list takes the width. The action sentence moves to
       the footer, which keeps the one promise the layout makes. */
    Screen.narrow #detail { display: none; }
    Screen.narrow #list { width: 1fr; border-right: none; }
    #overlay {
        width: 78;
        height: auto;
        max-height: 90%;
        background: $ad-raised;
        border: solid $ad-rule;
        padding: 1 3;
    }
    #overlay-title { color: $ad-text; text-style: bold; padding-bottom: 1; }
    #overlay-hint { color: $ad-text-dim; padding-top: 1; }
    #search, #dirinput { margin: 0 3; border: none; background: $ad-raised; }
    """

    # The footer shows four of these. Everything else is real but lives behind
    # ?, because a footer with twelve keys on it is a footer nobody reads.
    BINDINGS = [
        Binding("q", "quit", "quit", show=False),
        Binding("slash", "search", "search", show=False),
        Binding("n", "new_session", "new session", show=False),
        Binding("question_mark", "keymap", "keys", show=False),
        Binding("1", "filter_all", "all", show=False),
        Binding("2", "filter_claude", "claude", show=False),
        Binding("3", "filter_codex", "codex", show=False),
        Binding("4", "filter_opencode", "opencode", show=False),
        Binding("5", "filter_gemini", "gemini", show=False),
        Binding("c", "new_claude", "new claude", show=False),
        Binding("x", "new_codex", "new codex", show=False),
        Binding("o", "new_opencode", "new opencode", show=False),
        Binding("g", "new_gemini", "new gemini", show=False),
        Binding("t", "new_terminal", "terminal", show=False),
        Binding("y", "copy_resume", "copy resume command", show=False),
        Binding("u", "usage", "usage", show=False),
        Binding("T", "toggle_theme", "light / dark", show=False),
        Binding("tab", "switch_plan", "switch plan", priority=True),
        Binding("C", "clear", "clear history", show=False),
        Binding("U", "undo", "undo", show=False),
        Binding("r", "refresh", "refresh", show=False),
        Binding("escape", "escape", show=False),
    ]

    #: What ? shows, in the order it shows it. Kept next to BINDINGS so the two
    #: are edited together and the overlay can never go stale.
    KEYMAP_HELP = (
        ("moving around", [
            ("\u2191 \u2193", "move through the list"),
            ("\u23ce", "do what the detail pane says"),
            ("/", "search titles, prompts and projects"),
            ("1\u20135", "show all / claude / codex / opencode / gemini"),
        ]),
        ("starting work", [
            ("n", "new session"),
            ("c x o g t", "new claude / codex / opencode / gemini / shell"),
            ("tab", "switch the active claude plan"),
        ]),
        ("the selected row", [
            ("y", "copy the resume command"),
        ]),
        ("the dashboard", [
            ("u", "subscription usage"),
            ("T", "theme: auto / light / dark"),
            ("r", "refresh now"),
            ("C / U", "clear history / undo"),
            ("q", "quit"),
        ]),
    )

    def __init__(self, cmd_file: str | None = None, limit: int = 300, theme_name: str | None = None):
        # set before super(): App.__init__ builds the stylesheet, which calls
        # get_css_variables, which needs to know which palette we are on
        self.theme_name = theme_name or _load_theme_pref()
        self._system_theme = system_theme()
        super().__init__()
        self.cmd_file = cmd_file
        self.limit = limit
        self.sessions: list[Session] = []
        self.filtered: list[Session] = []
        self.tool_filter = "all"
        self.query = ""
        self.selected: Session | None = None
        self.usages: list[ToolUsage] = []
        self.usage_fetched_at: datetime | None = None
        self.running: list[RunningAgent] = []
        self.agent_titles: dict[int, str] = {}  # pid -> title, see _resolve_agent_titles
        # generated-title pipeline (see titles.py): the store persists titles by
        # session id; in_flight guards against re-queuing a session mid-generation.
        self._title_store = TitleStore()
        self._titling_in_flight: set[str] = set()
        # one list, one cursor: entries is agents-then-sessions, cursor indexes
        # it, and mode says whether the detail pane is describing the selection
        # or standing in as the launcher.
        self.entries: list[Entry] = []
        self.cursor = 0
        self.mode = "browse"  # "browse" | "launcher"
        self._line_index: dict[int, int] = {}  # list line -> entry index, for clicks
        self._loaded = False  # first history load done: separates "scanning" from "empty"
        # Until you move the cursor yourself it stays pinned to the top row, so
        # a live agent arriving a second after boot gets the selection instead
        # of leaving you looking at whatever history happened to load first.
        # Once you have moved it, identity preservation takes over and nothing
        # is allowed to shift it.
        self._cursor_moved = False
        self.launch_idx = 0  # starts on claude
        self.dirs: list[tuple[str, int, datetime | None]] = []  # directory rows
        self.picker_idx = 0  # highlighted directory; enter opens exactly this one
        self.picker_opened: list[str] = []  # dirs opened this run, echoed in the pane
        self._spin = 0  # spinner frame for working agents
        self.active_claude: str | None = None  # active claude plan label (multi-account)
        # Clearing history only hides sessions for the current run. It used to
        # persist to disk and reload on every launch, so an accidental C wiped
        # the view permanently with no obvious way back.
        self.cleared_at: datetime | None = None
        self._boot = 1.0
        self._boot_started = False
        self._boot_timer = None

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Horizontal(id="body"):
            with VerticalScroll(id="list"):
                yield _ListPane(id="listbody")
            with Vertical(id="detail"):
                with VerticalScroll(id="detailscroll"):
                    yield Static(id="detailbody")
                yield Static(id="action")
        yield Static(id="footer")
        yield Input(placeholder="search titles, prompts, projects\u2026  (esc to close)", id="search")
        yield Input(placeholder="directory path to open\u2026  (~ ok \u00b7 enter opens \u00b7 esc cancels)", id="dirinput")

    # ------------------------------------------------------------- theming

    def get_css_variables(self) -> dict[str, str]:
        """Textual resolves $ad-* from here, so switching themes is one dict
        swap plus a refresh_css() rather than a second stylesheet."""
        variables = super().get_css_variables()
        variables.update(bridge.css_variables(self.theme_obj))
        return variables

    @property
    def resolved_theme(self) -> str:
        """The palette actually in use. `auto` follows the system appearance."""
        if self.theme_name == "auto":
            return self._system_theme
        return self.theme_name

    @property
    def theme_obj(self) -> bridge.Theme:
        return bridge.THEMES.get(self.resolved_theme, bridge.THEMES[bridge.DEFAULT_THEME])

    def _follow_system_theme(self) -> None:
        """Re-read the system appearance and restyle if it flipped. Runs on the
        history poll rather than a timer of its own: switching your Mac between
        light and dark is a once-a-day event, not something worth a subprocess
        every second."""
        if self.theme_name != "auto":
            return
        current = system_theme()
        if current != self._system_theme:
            self._system_theme = current
            self.refresh_css()
            self._render_all()

    def on_mount(self) -> None:
        for widget_id in ("#search", "#dirinput"):
            field = self.query_one(widget_id, Input)
            field.display = False
            field.can_focus = False  # only focusable while it is open
        # Textual makes a scrollable container focusable, and a focused
        # container handles the arrow keys itself before they ever reach
        # on_key — which scrolled the page out from under the cursor as soon
        # as the list was long enough to have a scrollbar. Both panes are
        # driven entirely from on_key, so neither may take focus.
        for pane_id in ("#list", "#detailscroll"):
            self.query_one(pane_id).can_focus = False
        self.set_focus(None)  # keys flow to on_key / bindings, not a focused widget
        self.dirs = self._recent_dirs()
        self._apply_width()
        self._render_all()
        self.load_sessions()
        self.load_usage()
        self.load_running()
        # live activity polls fast; usage is slow (API); the spinner animates
        # locally between polls so "working" rows feel alive.
        self.set_interval(ACTIVE_POLL_SECONDS, self.load_running)
        self.set_interval(HISTORY_POLL_SECONDS, self.load_sessions)
        self.set_interval(USAGE_POLL_SECONDS, self.load_usage)
        self.set_interval(0.12, self._animate_active)

    def _animate_active(self) -> None:
        if any(a.state == "working" for a in self.running):
            self._spin += 1
            self._render_list()

    def _apply_width(self) -> None:
        """Two panes need room. Rather than tell you your terminal is wrong,
        drop the detail pane and give the list the width; the action sentence
        moves into the footer so Enter still announces itself."""
        self.screen.set_class((self.size.width or 120) < NARROW_COLUMNS, "narrow")

    @property
    def narrow(self) -> bool:
        return (self.size.width or 120) < NARROW_COLUMNS

    # ------------------------------------------------------------- rendering

    def _render_all(self) -> None:
        self._render_header()
        self._render_list()
        self._render_detail()
        self._render_footer()

    def _update(self, widget_id: str, markup: str) -> None:
        """Set a panel's contents, tolerating a poll that lands mid-teardown."""
        try:
            self.query_one(widget_id, Static).update(markup)
        except NoMatches:
            pass

    def _render_header(self) -> None:
        theme = self.theme_obj
        if self.query:
            self._update("#header", bridge.search_header(
                theme, self.query, len(self.filtered), len(self.sessions)))
            return
        name = f"[{theme.text}][bold]agent-dash[/bold][/]"
        chips = []
        for tool in ("all",) + TOOLS:
            if tool == self.tool_filter:
                chips.append(f"[{theme.accent}][bold]{tool}[/bold][/]")
            else:
                chips.append(f"[{theme.text_dim}]{tool}[/]")
        right = f"[{theme.text_dim}]/ to search[/]"
        if self.cleared_at:
            right = f"[{theme.waiting}]history cleared \u00b7 U restores[/]"
        left = f"{name}  " + "  ".join(chips)
        self._update("#header", f"{left}    {right}")

    def _list_width(self) -> int:
        try:
            return max(30, self.query_one("#list").size.width - 3)
        except (NoMatches, AttributeError):
            return 50

    def _render_list(self) -> None:
        theme = self.theme_obj
        width = self._list_width()
        lines: list[str] = []
        self._line_index = {}
        seen_kind = None
        for index, entry in enumerate(self.entries):
            if entry.kind != seen_kind:
                if seen_kind is not None:
                    lines.append("")
                heading = "running now" if entry.kind == "agent" else "earlier"
                lines.append(bridge.label(heading, theme, strong=(entry.kind == "agent")))
                lines.append("")
                seen_kind = entry.kind
            selected = index == self.cursor and self.mode == "browse"
            if entry.kind == "agent":
                row = bridge.agent_row(entry.agent, self._title_for(entry.agent),
                                       selected=selected, theme=theme, tick=self._spin)
            else:
                row = bridge.session_row(entry.session, selected=selected)
            rendered = bridge.render_row(row, theme, width)
            for offset in range(len(rendered.splitlines())):
                self._line_index[len(lines) + offset] = index
            lines.extend(rendered.splitlines())
            lines.append("")
        if not self.entries:
            lines = [f"[{theme.text_dim}]{'scanning\u2026' if not self._loaded else 'nothing here yet'}[/]"]
        self._update("#listbody", "\n".join(lines))

    def _entry_at_line(self, line: int) -> int | None:
        return self._line_index.get(line)

    @property
    def current(self) -> Entry | None:
        if 0 <= self.cursor < len(self.entries):
            return self.entries[self.cursor]
        return None

    def _usage_note(self, tool: str) -> str:
        """The one usage figure that is relevant to what is selected. The full
        grid lives behind u; a dashboard that shows you every window all the
        time is showing you four numbers you did not ask for."""
        for usage in self.usages:
            if usage.tool != tool:
                continue
            windows = [w for w in usage.windows if w.pct is not None]
            if not windows:
                return usage.note or ""
            window = windows[-1]
            reset = _fmt_reset_compact(window.resets_at)
            return f"{window.label} {window.pct:.0f}%" + (f" \u00b7 resets {reset}" if reset else "")
        return ""

    def _render_detail(self) -> None:
        theme = self.theme_obj
        if self.mode == "launcher":
            self._update("#detailbody", bridge.launcher(
                theme, LAUNCH_TOOLS, self.launch_idx, self.dirs, self.picker_idx,
                self.picker_opened, plan_label=self._plan_label(),
                plan_hint=self._plan_hint(), include_action=False))
            sentence = bridge.launch_sentence(LAUNCH_TOOLS, self.launch_idx,
                                              self.dirs, self.picker_idx)
            self._update("#action", bridge.action_bar(
                theme, "\u23ce", sentence, "stays open for the next one"))
            return
        entry = self.current
        if entry is None:
            self._update("#detailbody", bridge.detail_empty(theme))
            self._update("#action", bridge.action_bar(theme, "n", "start an agent somewhere"))
            return
        width = self._detail_width()
        if entry.kind == "agent":
            agent = entry.agent
            session = self._session_for(agent)
            body = bridge.detail_running(agent, session, theme, self._usage_note(agent.tool),
                                         turns=self._turns(session), width=width)
            action = bridge.action_bar(theme, "\u23ce", "jump to this tab in Warp", agent.tty)
        else:
            session = entry.session
            body = bridge.detail_session(session, theme, self._usage_note(session.tool),
                                         turns=self._turns(session), width=width)
            action = bridge.action_bar(theme, "\u23ce", "resume in a new Warp tab",
                                       Path(resume_directory(session)).name)
        self._update("#detailbody", body)
        self._update("#action", action)
        self._scroll_detail_to_latest()

    def _detail_width(self) -> int:
        try:
            return max(30, self.query_one("#detailscroll").size.width - 2)
        except (NoMatches, AttributeError):
            return 76

    def _turns(self, session: Session | None):
        """The conversation behind the selection, or nothing if the transcript
        is gone. Never let a malformed log take the pane down with it."""
        if session is None:
            return ()
        try:
            return transcript.read_turns(session)
        except Exception:
            return ()

    def _scroll_detail_to_latest(self) -> None:
        """Park the pane on the newest turn. The conversation reads oldest to
        newest like scrollback, so the bottom is the interesting end — the same
        place the terminal itself would have left you."""
        try:
            pane = self.query_one("#detailscroll", VerticalScroll)
        except NoMatches:
            return
        pane.scroll_end(animate=False)

    def _render_footer(self) -> None:
        theme = self.theme_obj
        hints = [("\u23ce", self._action_words()), ("n", "new session"),
                 ("/", "search"), ("?", "all keys")]
        self._update("#footer", bridge.hint_line(theme, hints))

    def _action_words(self) -> str:
        """What Enter does, in three words. In a narrow terminal this is the
        only place the promise survives, so it is never allowed to be vague."""
        if self.mode == "launcher":
            return bridge.launch_sentence(LAUNCH_TOOLS, self.launch_idx,
                                          self.dirs, self.picker_idx)
        entry = self.current
        if entry is None:
            return "nothing selected"
        return "jump to the tab" if entry.kind == "agent" else "resume this session"

    def _session_for(self, agent: RunningAgent) -> Session | None:
        """The history row behind a live agent, so the detail pane can show the
        prompt and message count the process itself does not know."""
        if agent.session_id:
            for session in self.sessions:
                if session.id == agent.session_id:
                    return session
        for session in self.sessions:
            if session.tool == agent.tool and session.project_dir == agent.cwd:
                return session
        return None

    def _plan_label(self) -> str:
        labels = [u.label for u in self.usages if u.tool == "claude" and u.label]
        if not labels:
            return ""
        return self.active_claude or labels[0]

    def _plan_hint(self) -> str:
        labels = [u.label for u in self.usages if u.tool == "claude" and u.label]
        if len(labels) < 2:
            return ""
        active = self.active_claude or labels[0]
        nxt = labels[(labels.index(active) + 1) % len(labels)] if active in labels else labels[0]
        return f"tab switches to {nxt}"

    # ------------------------------------------------------------- the cursor

    def _cursor_key(self) -> str | None:
        entry = self.current
        return entry.key if entry else None

    def _restore_cursor(self, key: str | None) -> None:
        """Put the cursor back on the row it was on. The active poll rebuilds
        the list every two seconds and a new session arriving at the top would
        otherwise drag the selection onto a different row under your hands."""
        if not self._cursor_moved:
            self.cursor = 0  # you have not chosen anything yet; follow the top
            return
        if key is None:
            self.cursor = min(self.cursor, max(0, len(self.entries) - 1))
            return
        for index, entry in enumerate(self.entries):
            if entry.key == key:
                self.cursor = index
                return
        self.cursor = min(self.cursor, max(0, len(self.entries) - 1))

    def _rebuild(self) -> None:
        """Rebuild the single list: live agents first, then filtered history."""
        previous = self._cursor_key()
        self.filtered = [s for s in self.sessions if self._matches(s)]
        entries = [Entry("agent", f"agent:{a.tool}:{a.pid}", agent=a) for a in self.running]
        # A running agent and its own history row are the same piece of work.
        # The old screen listed both, once under "active now" and again at the
        # top of the table, so the first thing you did on every refresh was
        # read the same three sessions twice. Live wins; the row is dropped.
        live = {id(s) for s in (self._session_for(a) for a in self.running) if s is not None}
        entries += [Entry("session", f"session:{s.id}", session=s)
                    for s in self.filtered if id(s) not in live]
        self.entries = entries
        self._restore_cursor(previous)
        self._render_all()

    def _populate(self) -> None:
        """Kept as the name the history/search/filter paths already call."""
        self._rebuild()

    def _render_active(self) -> None:
        """Kept as the name the activity poll already calls."""
        self._rebuild()

    def _move(self, delta: int) -> None:
        if not self.entries:
            return
        self._cursor_moved = True
        self.cursor = max(0, min(len(self.entries) - 1, self.cursor + delta))
        self._render_list()
        self._render_detail()
        self._render_footer()
        self._scroll_to_cursor()

    def _scroll_to_cursor(self) -> None:
        """Keep the cursor in view, and otherwise leave the scroll alone.

        Arrowing down used to scroll on every press, so the list slid under a
        cursor that was pinned near the top of the pane. The rule people expect
        from a list is the opposite: the cursor moves through a still page, and
        the page only moves once the cursor would leave it.
        """
        try:
            pane = self.query_one("#list", VerticalScroll)
        except NoMatches:
            return
        lines = sorted(line for line, index in self._line_index.items() if index == self.cursor)
        if not lines:
            return
        top, bottom = lines[0], lines[-1]
        window_top = int(pane.scroll_offset.y)
        window_height = max(1, pane.size.height)
        window_bottom = window_top + window_height - 1
        if top < window_top:
            # come to rest two lines high so the section heading above the row
            # stays on screen; at the very top this lands cleanly on zero
            pane.scroll_to(y=max(0, top - 2), animate=False)
        elif bottom > window_bottom:
            pane.scroll_to(y=bottom - window_height + 1, animate=False)

    # ------------------------------------------------------------- keyboard

    def on_key(self, event) -> None:
        if len(self.screen_stack) > 1:
            return  # an overlay owns the keyboard
        if self.query_one("#search", Input).display:
            return  # the search box owns the keyboard while it is open
        if self.query_one("#dirinput", Input).display:
            return  # the path prompt owns the keyboard while it is open
        key = event.key
        if key not in ("enter", "left", "right", "up", "down"):
            return
        event.stop()
        if key == "enter":
            self.action_primary()
        elif key in ("left", "right"):
            if self.mode == "launcher":
                step = -1 if key == "left" else 1
                self.launch_idx = (self.launch_idx + step) % len(LAUNCH_TOOLS)
                self._render_detail()
                self._render_footer()
        elif key == "up":
            if self.mode == "launcher":
                self.picker_idx = max(0, self.picker_idx - 1)
                self._render_detail()
                self._render_footer()
            else:
                self._move(-1)
        elif key == "down":
            if self.mode == "launcher":
                self.picker_idx = min(len(self.dirs), self.picker_idx + 1)
                self._render_detail()
                self._render_footer()
            else:
                self._move(1)

    def action_primary(self) -> None:
        """Enter. Whatever the detail pane just promised, this is where it is
        kept — one entry point so the sentence and the behaviour cannot drift."""
        if self.mode == "launcher":
            if self.picker_idx >= len(self.dirs):
                self._open_dir_input()  # the "type a path" row
            else:
                self._launch(LAUNCH_TOOLS[self.launch_idx])
            return
        entry = self.current
        if entry is None:
            return
        if entry.kind == "agent":
            self._focus_agent(entry.agent)
        else:
            self.selected = entry.session
            self.action_resume()

    # ------------------------------------------------------------- active now

    @work(thread=True, exclusive=True, group="running")
    def load_running(self) -> None:
        agents = enrich(running_agents())
        self.call_from_thread(self._on_running, agents)

    def _on_running(self, agents: list[RunningAgent]) -> None:
        self.running = agents
        self._resolve_agent_titles()  # cached: the spinner tick re-renders too
        self._rebuild()

    def _title_for(self, agent: RunningAgent) -> str:
        return self.agent_titles.get(agent.pid) or Path(agent.cwd).name or "session"

    def _resolve_agent_titles(self) -> None:
        """Give every running agent its own session title (pid -> title).

        Claude records the transcript id in ~/.claude/sessions/<pid>.json, so
        those resolve exactly. Everything else falls back to tool + working
        directory, but a session is claimed by at most one agent: when two
        agents share a directory they pair newest-process to newest-transcript
        instead of all showing whichever session was touched last."""
        by_id = {s.id: s for s in self.sessions}
        titles: dict[int, str] = {}
        claimed: set[str] = set()
        unresolved: list[RunningAgent] = []
        for agent in self.running:
            session = by_id.get(agent.session_id) if agent.session_id else None
            if session:
                titles[agent.pid] = session.title
                claimed.add(session.id)
            else:
                unresolved.append(agent)
        groups: dict[tuple[str, str], list[RunningAgent]] = {}
        for agent in unresolved:
            groups.setdefault((agent.tool, agent.cwd or ""), []).append(agent)
        for (tool, cwd), members in groups.items():
            pool = sorted(
                (s for s in self.sessions
                 if s.tool == tool and cwd and s.project_dir == cwd and s.id not in claimed),
                key=lambda s: s.last_active,
                reverse=True,
            )
            members.sort(key=lambda a: a.pid, reverse=True)  # newest process first
            for agent, session in zip(members, pool):
                titles[agent.pid] = session.title
                claimed.add(session.id)
        self.agent_titles = titles

    @work(thread=True, exclusive=True, group="focus")
    def _focus_agent(self, agent: RunningAgent) -> None:
        """Jump to the Warp tab running this agent (applescript is slow: thread)."""
        title = self._title_for(agent)
        hints = [title[:24], Path(agent.cwd).name]
        result = _focus_warp_tab(hints)
        self.call_from_thread(self._notify_focus_result, agent, result)

    def _notify_focus_result(self, agent: RunningAgent, result: str) -> None:
        if result == "focused":
            self.notify(f"jumped to the {agent.tool} tab", timeout=2)
        elif result == "denied":
            self.notify(
                "needs accessibility access to find the tab: allow Warp in "
                "System Settings → Privacy & Security → Accessibility",
                timeout=8,
            )
            Popen(["open", ACCESSIBILITY_URL], stdout=DEVNULL, stderr=DEVNULL)
        else:
            self.notify(f"Warp is up front; look for the tab on {agent.tty}", timeout=3)

    # ------------------------------------------------------------- usage

    @work(thread=True)
    def load_usage(self, force: bool = False) -> None:
        usages, fetched_at = collect_usage(self.active_claude, force=force)
        self.call_from_thread(self._on_usage, usages, fetched_at)

    def _on_usage(self, usages: list[ToolUsage], fetched_at: datetime) -> None:
        self.usages = usages
        self.usage_fetched_at = fetched_at
        # adopt the fetched active plan on first load so `tab` has a starting point
        if self.active_claude is None:
            self.active_claude = next(
                (u.label for u in usages if u.tool == "claude" and u.label and u.active), None
            )
        # the gauges only mean anything once they have data, so the self-test
        # sweep waits for the first load rather than firing on an empty panel
        self._boot = 1.0  # the sweep belongs to the usage overlay, not the shell
        self._render_all()

    def _usage_prefix(self, usage: ToolUsage) -> str:
        """Marker + tool + plan columns, padded to a fixed visible width."""
        color = TOOL_COLORS[usage.tool]
        if usage.label:  # multi-account claude: active/inactive marker + label
            marker = "[#7aa2f7]◉[/]" if usage.active else "[#55647f]○[/]"
            plan_text = f"{usage.plan}·{usage.label}" if usage.plan else usage.label
        else:
            marker = f"[{color}]●[/{color}]"
            plan_text = usage.plan
        tool_seg = _pad_visible(f"[{color}]{usage.tool}[/{color}]", len(usage.tool), USAGE_TOOL_W)
        plan_disp = _truncate(plan_text, USAGE_PLAN_W - 1)
        plan_seg = _pad_visible(f"[dim]{escape(plan_disp)}[/]", len(plan_disp), USAGE_PLAN_W)
        return f"{marker} {tool_seg}{plan_seg}"

    def _usage_text(self, width: int | None = None) -> str:
        # Codex can report only its weekly window. Build the grid from the
        # windows actually present instead of showing a permanent empty 5h cell.
        reported = {w.label for usage in self.usages for w in usage.windows}
        if any(usage.spend is not None for usage in self.usages):
            reported.add("7d")
        labels = [label for label in ("5h", "7d") if label in reported]
        labels.extend(
            label for label in dict.fromkeys(
                w.label for usage in self.usages for w in usage.windows
                if w.label not in ("5h", "7d")
            )
        )
        # room left for a stale marker after the bars; the grid comes first, so a
        # narrow terminal drops the marker rather than wrapping the row
        cells = len(labels)
        marker_room = (width or 200) - (2 + USAGE_TOOL_W + USAGE_PLAN_W
                                        + cells * (USAGE_CELL_W + len(USAGE_GAP)))
        cols = USAGE_GAP.join(_pad_visible(label, len(label), USAGE_CELL_W) for label in labels)
        header = _pad_visible("", 0, 2) + "[dim]" + (
            _pad_visible("plan", 4, USAGE_TOOL_W + USAGE_PLAN_W) + cols
        ) + "[/]"
        lines = [header]
        for usage in self.usages:
            prefix = self._usage_prefix(usage)
            if usage.error:
                lines.append(f"{prefix}[dim]{escape(usage.error)}[/]")
                continue
            if usage.spend is not None:  # BYOK: dollar spend fills the 7d column
                empty = _pad_visible("[#55647f]—[/]", 1, USAGE_CELL_W)
                sess = usage.spend_sessions or 0
                spend_cell = (
                    f"[#c7d4f0]${usage.spend:.2f}[/] "
                    f"[dim]· {sess} session{'s' if sess != 1 else ''} · {usage.spend_days}d[/]"
                )
                cells = [spend_cell if label == "7d" else empty for label in labels]
                lines.append(f"{prefix}{USAGE_GAP.join(cells)}")
                continue
            windows = {w.label: w for w in usage.windows}
            p = self._boot
            row = f"{prefix}{USAGE_GAP.join(_window_cell(windows.get(label), p) for label in labels)}"
            if usage.stale and marker_room >= 6:  # cached bars: say so after them
                mark = _truncate(usage.stale, marker_room)
                row += f"{USAGE_GAP}[dim]{escape(mark)}[/]"
            lines.append(row)
        multi = sum(1 for u in self.usages if u.tool == "claude" and u.label) > 1
        if multi:
            lines.append("[dim]↹ tab switches the active claude plan[/]")
        return "\n".join(lines)

    def _render_usage(self) -> None:
        """Usage no longer has a permanent panel: the detail pane carries the one
        window that matters to the selection, and `u` opens the full grid."""
        self._render_detail()

    # ------------------------------------------------------------- history

    @work(thread=True)
    def load_sessions(self) -> None:
        sessions = collect_all(limit=self.limit)
        self.call_from_thread(self._on_loaded, sessions)

    def _on_loaded(self, sessions: list[Session]) -> None:
        # overlay cached generated titles before render so rows show the canonical
        # title immediately; eligible is the set still needing (re)generation.
        eligible = apply_titles(sessions, self._title_store)
        self.sessions = sessions
        self.dirs = self._recent_dirs()
        self.picker_idx = min(self.picker_idx, len(self.dirs))  # len(dirs) == the "type a path" row
        self._loaded = True
        self._follow_system_theme()
        self._rebuild()
        self.load_running()
        self._schedule_titles(eligible)

    def _schedule_titles(self, eligible: list[Session]) -> None:
        """Queue a bounded batch of sessions for title generation. Only one batch
        runs at a time (skipped while any is in flight) so codex usage stays
        capped no matter how big the backlog is."""
        if self._titling_in_flight or not eligible:
            return
        batch = eligible[:TITLE_BATCH]
        for session in batch:
            self._titling_in_flight.add(session.id)
        self._generate_titles(batch)

    @work(thread=True, group="titling")
    def _generate_titles(self, batch: list[Session]) -> None:
        from concurrent.futures import ThreadPoolExecutor

        def one(session: Session) -> tuple[str, str, int, str]:
            try:
                title = generate_title(session)
            except Exception:
                # never let a single failure leave the session stuck in_flight
                title = session.title
            return session.id, title, session.n_messages, session.tool

        # at most 2 concurrent codex processes; results stream back as they land
        with ThreadPoolExecutor(max_workers=2) as pool:
            for session_id, title, n_messages, tool in pool.map(one, batch):
                self.call_from_thread(self._on_title, session_id, title, n_messages, tool)

    def _on_title(self, session_id: str, title: str, n_messages: int, tool: str) -> None:
        self._title_store.put(session_id, title, n_messages, tool)
        self._titling_in_flight.discard(session_id)
        for session in self.sessions:
            if session.id == session_id:
                session.title = title
                break
        self._populate()
        # a running agent may map to this session; refresh its active-now row
        if self.running:
            self._resolve_agent_titles()
            self._render_active()

    def _matches(self, session: Session) -> bool:
        if self.tool_filter != "all" and session.tool != self.tool_filter:
            return False
        if self.cleared_at and session.last_active < self.cleared_at:
            return False
        if self.query:
            haystack = f"{session.title} {session.first_prompt} {session.project_dir}".lower()
            if self.query.lower() not in haystack:
                return False
        return True

    # ------------------------------------------------------------- events

    @on(Input.Changed, "#search")
    def _on_search_changed(self, event: Input.Changed) -> None:
        self.query = event.value
        self._populate()

    @on(Input.Submitted, "#search")
    def _on_search_submitted(self) -> None:
        self._close_search()

    # ------------------------------------------------------------- actions

    def action_resume(self) -> None:
        session = self.selected
        if session is None:
            entry = self.current
            if entry is None or entry.kind != "session":
                return
            session = self.selected = entry.session
        # Resume in a fresh Warp tab, the same way the launcher opens new
        # sessions, so the dashboard stays up and you never lose it to the
        # resumed session taking over this terminal.
        cwd = resume_directory(session)
        if self._open_tab(session.tool, cwd,
                          suffix=f"resume-{session.id[:8]}",
                          command=resume_invocation(session)):
            self.notify(f"resuming {session.tool} in {Path(cwd).name or cwd}", timeout=2)

    def _recent_dirs(self, limit: int = 8) -> list[tuple[str, int, datetime | None]]:
        """Recent working directories from session history, most-recent first,
        with the current directory pinned to the top."""
        count: dict[str, int] = {}
        last: dict[str, datetime] = {}
        for s in self.sessions:
            d = s.project_dir
            if not d or _is_worktree(d):  # never offer to start fresh in a worktree
                continue
            count[d] = count.get(d, 0) + 1
            if d not in last or s.last_active > last[d]:
                last[d] = s.last_active
        ranked = sorted(count, key=lambda d: last[d], reverse=True)
        cwd = os.getcwd()
        if not _is_worktree(cwd):  # the pin honors the same exclusion as history
            ranked = [cwd] + [d for d in ranked if d != cwd]
        return [(d, count.get(d, 0), last.get(d)) for d in ranked[:limit]]

    def _active_claude_profile(self):
        profiles = claude_profiles()
        for p in profiles:
            if p.label == self.active_claude:
                return p
        return profiles[0]

    def _selected_dirs(self) -> list[str]:
        """The highlighted directory. On the "other directory" row (or with no
        history yet) fall back to the top row, which is always the cwd."""
        if self.picker_idx < len(self.dirs):
            return [self.dirs[self.picker_idx][0]]
        return [self.dirs[0][0]] if self.dirs else [os.getcwd()]

    def _launch(self, tool: str, dirs: list[str] | None = None) -> None:
        """Open a Warp tab per directory running `tool` (a bare shell for
        `terminal`). Defaults to the picker's selected dirs; a caller can pass an
        explicit list (e.g. a typed-in path). Claude uses the active plan's
        CLAUDE_CONFIG_DIR."""
        dirs = dirs if dirs is not None else self._selected_dirs()
        env: dict = {}
        plan = ""
        if tool == "claude":
            profile = self._active_claude_profile()
            if not profile.default:
                env = {"CLAUDE_CONFIG_DIR": str(profile.config_dir)}
                plan = f" ({profile.label})"
        opened = []
        for d in dirs:
            if self._open_tab(tool, d, suffix=_dir_slug(d), env=env):
                opened.append(d)
                if d not in self.picker_opened:
                    self.picker_opened.append(d)
        if len(opened) == len(dirs):
            where = ", ".join(Path(d).name or d for d in dirs)
            self.notify(f"opening {tool}{plan} in {where}", timeout=2)
        self._render_detail()  # the pane echoes what has been opened

    def _open_tab(self, tool: str, cwd: str, suffix: str = "", env: dict | None = None, command: str | None = None) -> bool:
        try:
            stem = _write_tab_config(tool, cwd, _warp_configs_dir(), suffix=suffix, env=env, command=command)
            Popen(["open", f"warp://tab_config/{stem}"], stdout=DEVNULL, stderr=DEVNULL)
            return True
        except OSError as exc:
            self.notify(f"could not open tab: {exc}", severity="error", timeout=4)
            return False

    def action_new_claude(self) -> None:
        self._launch("claude")

    def action_new_codex(self) -> None:
        self._launch("codex")

    def action_new_opencode(self) -> None:
        self._launch("opencode")

    def action_new_gemini(self) -> None:
        self._launch("gemini")

    def action_new_terminal(self) -> None:
        self._launch("terminal")

    def action_switch_plan(self) -> None:
        """Cycle which claude plan new sessions use (multi-account only)."""
        labels = [u.label for u in self.usages if u.tool == "claude" and u.label]
        if len(labels) < 2:
            return
        cur = self.active_claude if self.active_claude in labels else labels[0]
        self.active_claude = labels[(labels.index(cur) + 1) % len(labels)]
        for usage in self.usages:
            if usage.tool == "claude" and usage.label:
                usage.active = usage.label == self.active_claude
        self._render_usage()
        self.notify(f"claude plan: {self.active_claude}", timeout=2)

    def action_new_session(self) -> None:
        """n. The launcher takes over the detail pane rather than opening a
        modal, so the list stays visible and you keep your bearings."""
        self.mode = "launcher"
        self._render_all()

    def action_keymap(self) -> None:
        theme = self.theme_obj
        blocks = []
        for heading, rows in self.KEYMAP_HELP:
            blocks.append(bridge.label(heading, theme))
            for key, what in rows:
                blocks.append(
                    f"  [{theme.text}][bold]{key.ljust(10)}[/bold][/] [{theme.text_soft}]{what}[/]"
                )
            blocks.append("")
        self.push_screen(_OverlayScreen("\n".join(blocks).rstrip(), "Keys"))

    def action_usage(self) -> None:
        """u. The full subscription grid on demand. It used to sit on screen
        permanently, which meant four numbers competing with the thing you came
        here to look at."""
        body = self._usage_text(72) if self.usages else "[dim]usage has not loaded yet[/]"
        self.push_screen(_OverlayScreen(body, "Subscriptions"))

    def action_toggle_theme(self) -> None:
        """auto -> light -> dark -> auto. `auto` is first because following the
        system is the setting you want unless you have a reason not to."""
        nxt = THEME_PREFS[(THEME_PREFS.index(self.theme_name) + 1) % len(THEME_PREFS)]
        self.theme_name = nxt
        if nxt == "auto":
            self._system_theme = system_theme()
        _save_theme_pref(nxt)
        self.refresh_css()
        self._render_all()
        told = f"following the system ({self.resolved_theme})" if nxt == "auto" else f"{nxt} mode"
        self.notify(told, timeout=2)

    def action_copy_resume(self) -> None:
        """y. Copies the exact command the detail pane is showing, so the pane
        is not just describing something you then have to retype."""
        entry = self.current
        if entry is None or entry.kind != "session":
            return
        command = resume_invocation(entry.session)
        try:
            self.copy_to_clipboard(command)
        except Exception:
            pass
        self.notify(f"copied: {command}", timeout=3)

    def action_clear(self) -> None:
        self.cleared_at = datetime.now(timezone.utc)
        self._populate()
        self.notify("history cleared — press u to restore", timeout=3)

    def action_undo(self) -> None:
        if not self.cleared_at:
            return
        self.cleared_at = None
        self._populate()
        self.notify("history restored", timeout=2)

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.can_focus = True
        search.display = True
        search.focus()

    def _close_search(self) -> None:
        search = self.query_one("#search", Input)
        search.value = ""
        search.display = False
        search.can_focus = False
        self.query = ""
        self.set_focus(None)
        self._populate()

    def _open_dir_input(self) -> None:
        """Prompt for a directory path to open that isn't in the recent list."""
        dirinput = self.query_one("#dirinput", Input)
        dirinput.value = ""
        dirinput.can_focus = True
        dirinput.display = True
        dirinput.focus()

    def _close_dir_input(self) -> None:
        dirinput = self.query_one("#dirinput", Input)
        dirinput.value = ""
        dirinput.display = False
        dirinput.can_focus = False
        self.set_focus(None)

    @on(Input.Submitted, "#dirinput")
    def _on_dir_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        self._close_dir_input()
        if not raw:
            return
        path = Path(raw).expanduser()
        if not path.is_dir():
            self.notify(f"not a directory: {raw}", severity="error", timeout=4)
            return
        self._launch(LAUNCH_TOOLS[self.launch_idx], dirs=[str(path)])

    def action_escape(self) -> None:
        if self.query_one("#search", Input).display:
            self._close_search()
        elif self.query_one("#dirinput", Input).display:
            self._close_dir_input()
        elif self.mode == "launcher":
            self.mode = "browse"
            self._render_all()

    def _set_filter(self, tool: str) -> None:
        self.tool_filter = tool
        self._populate()

    def action_filter_all(self) -> None:
        self._set_filter("all")

    def action_filter_claude(self) -> None:
        self._set_filter("claude")

    def action_filter_codex(self) -> None:
        self._set_filter("codex")

    def action_filter_opencode(self) -> None:
        self._set_filter("opencode")

    def action_filter_gemini(self) -> None:
        self._set_filter("gemini")

    def action_refresh(self) -> None:
        self.load_sessions()
        self.load_usage(force=True)

    def on_resize(self) -> None:
        self._apply_width()
        self._render_all()
