"""Textual TUI for adash: subscription usage + active agents + session history."""

from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import DEVNULL, Popen

from rich.markup import escape
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Input, Static

from activity import enrich
from agents import RunningAgent, running_agents
from collectors import collect_all
from models import TOOL_COLORS, Session, fmt_tokens, rel_time, resume_command
from usage import ToolUsage, UsageWindow, claude_profiles, collect_usage

BAR_WIDTH = 24
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
MAX_ACTIVE_ROWS = 6
BANNER_TEXT = "JosephCode"
BANNER_FONTS = ("slant", "small")  # widest first; each is measured before use
# columns never available to the banner: its own padding (2 per side) plus the
# screen's vertical scrollbar, assumed always present so the art still fits if
# the scrollbar pops in after data loads
BANNER_CHROME = 6
# the launcher can start any agent CLI, or just a plain shell ("terminal")
LAUNCH_TOOLS = ("claude", "codex", "opencode", "terminal")
# braille spinner frames for the "working" indicator, like the CLIs themselves
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ACTIVE_POLL_SECONDS = 2.0  # live activity refresh (cheap: file reads + one ps)
# Usage refresh. Claude hits a rate-limited API once per account here, and every
# running Claude Code session polls the same endpoint against the same quota, so
# this stays slow on purpose: the 5h/7d bars move far too gradually to be worth
# a tighter loop. usage.py caches on top of this and backs off on a 429.
USAGE_POLL_SECONDS = 180.0


def _banner_art(width: int) -> str:
    """Figlet art for the banner that fits in `width` columns, or "" when no
    font does. Art that is even one column too wide gets word-wrapped by the
    Static, which shreds it into diagonal fragments, so measure before using."""
    try:
        import pyfiglet

        for font in BANNER_FONTS:
            art = pyfiglet.figlet_format(BANNER_TEXT, font=font).rstrip()
            if art and max(len(line) for line in art.splitlines()) <= width:
                return art
    except Exception:
        pass
    return ""


def _truncate(text: str, width: int) -> str:
    return text[: width - 1] + "…" if len(text) > width else text


def _usage_color(pct: float) -> str:
    # instrument palette: calm blue until it runs warm, then amber, then red
    return "#7aa2f7" if pct < 60 else "#ffb454" if pct < 85 else "#ff5c57"


def _bar(pct: float | None, width: int = BAR_WIDTH) -> str:
    if pct is None:
        return f"[dim]{'░' * width} n/a[/]"
    filled = round(width * min(pct, 100.0) / 100.0)
    return f"[{_usage_color(pct)}]{'█' * filled}[/][#1b2233]{'░' * (width - filled)}[/] {pct:.0f}%"


def _fmt_reset_compact(dt: datetime | None) -> str:
    """Reset time squeezed to fit a usage column: '10:59a', '5:30p', 'Jul24'."""
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    local = dt.astimezone()
    if dt - now < timedelta(hours=24):
        return local.strftime("%-I:%M%p").lower().replace("am", "a").replace("pm", "p")
    return local.strftime("%b%d")


def _usage_bar(pct: float | None) -> str:
    """Mini bar of exactly USAGE_BAR_W visible columns for the aligned grid."""
    if pct is None:
        return f"[#2a3348]{'░' * USAGE_BAR_W}[/]"
    filled = round(USAGE_BAR_W * min(pct, 100.0) / 100.0)
    return f"[{_usage_color(pct)}]{'█' * filled}[/][#1b2233]{'░' * (USAGE_BAR_W - filled)}[/]"


def _window_cell(win: "UsageWindow | None") -> str:
    """A fixed-width (USAGE_CELL_W) cell: mini-bar + percent + compact reset.

    A missing window (a tool that doesn't report this limit) renders as an empty
    bar with a dim dash, so the column still lines up with its neighbors.
    """
    pct = win.pct if win else None
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


def _status_marker(state: str, frame: str, tool_color: str) -> str:
    """Leading glyph for an active-now row based on live state."""
    if state == "working":
        return f"[#7aa2f7]{frame}[/]"
    if state == "waiting":
        return "[#ffb454]●[/]"
    if state == "idle":
        return "[#55647f]○[/]"
    return f"[{tool_color}]●[/{tool_color}]"  # unknown: fall back to the tool dot


def _activity_detail(agent) -> str:
    """Second-line detail (current action + live tokens) for a busy agent."""
    if agent.state not in ("working", "waiting"):
        return ""
    bits = []
    if agent.state == "waiting":
        bits.append(f"[#ffb454]{escape(agent.label or 'waiting for input')}[/]")
    elif agent.label:
        bits.append(f"[#7aa2f7]{escape(agent.label)}[/]")
    if agent.tokens:
        bits.append(f"[dim]{fmt_tokens(agent.tokens)} tokens[/]")
    return " · ".join(bits)


_TAB_COLORS = {"claude": "magenta", "codex": "green", "opencode": "blue", "terminal": "blue"}
# None means "open a plain shell in the directory" — no agent command is run
_TAB_COMMANDS = {"claude": "claude", "codex": "codex", "opencode": "opencode", "terminal": None}


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


def _write_tab_config(tool: str, cwd: str, configs_dir: Path, suffix: str = "", env: dict | None = None) -> str:
    """Generate (or refresh) a tab config that opens `tool` in `cwd`; return its stem.

    `suffix` distinguishes tabs launched in different directories (e.g. opening
    claude in several repos at once). `env` prepends VAR=value assignments to the
    command (used to point claude at a non-default CLAUDE_CONFIG_DIR account).
    When the tool has no command (terminal), the tab opens a bare shell.
    """
    stem = f"josephcode-{tool}" + (f"-{suffix}" if suffix else "")
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
    command = _TAB_COMMANDS[tool]
    if command:
        prefix = "".join(f"{k}={v} " for k, v in (env or {}).items())
        lines.append(f'commands = ["{prefix}{command}"]')
    content = "\n".join(lines) + "\n"
    target = configs_dir / f"{stem}.toml"
    if not target.exists() or target.read_text() != content:
        configs_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return stem


# ---------------------------------------------------------------- cleared-history marker


def _cleared_file() -> Path:
    return Path.home() / ".cache" / "adash" / "cleared-at"


def _load_cleared() -> datetime | None:
    try:
        text = _cleared_file().read_text().strip()
        return datetime.fromisoformat(text) if text else None
    except (OSError, ValueError):
        return None


def _save_cleared(value: datetime | None) -> None:
    try:
        path = _cleared_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value.isoformat())
    except OSError:
        pass


class _SessionsTable(DataTable):
    """History table that never takes keyboard focus.

    All navigation is driven by AdashApp.on_key so the launcher/active/history
    zones share one selection chain. Under Textual 8 a focusable DataTable gets
    auto-focused at mount and its built-in cursor keys fire alongside on_key,
    double-moving the cursor and desyncing the zone state. Keeping it
    unfocusable (together with clearing focus at mount and only focusing the
    search box while it is open) means every key reaches on_key or a binding.
    """

    can_focus = False


class _ActivePanel(Static):
    """Active-now panel where clicking a row jumps to that agent's Warp tab.

    The click is handled here rather than in AdashApp.on_click: under Textual 8
    mouse events are delivered to the widget under the cursor and don't reach
    app-level handlers the way keys do. event.y is relative to the panel's
    border, so content rows start at y=2 (border + top padding).
    """

    def on_click(self, event: events.Click) -> None:
        app = self.app
        if not isinstance(app, AdashApp) or not app.running:
            return
        idx = app._agent_at_line(event.y - 2)
        if idx is None:
            return
        app.active_idx = idx
        app._set_zone("active")
        app._focus_agent(app.running[idx])


class AdashApp(App):
    TITLE = "JosephCode"

    CSS = """
    Screen {
        background: #0a0c12;
        color: #c7d4f0;
    }
    #banner {
        height: auto;
        padding: 1 2 0 2;
        text-align: center;
        color: #7aa2f7;
    }
    .panel {
        height: auto;
        border: round #1b2233;
        padding: 1 2;
        margin: 0 1;
    }
    #launch {
        height: auto;
        padding: 1 2 0 2;
    }
    #picker {
        height: auto;
        padding: 0 2 1 4;
    }
    #sessions {
        height: 1fr;
        min-height: 6;
        margin: 0 1 1 1;
        border: solid #1b2233;
    }
    #sessions > .datatable--cursor {
        background: #16305e;
        color: #ffffff;
    }
    #sessions > .datatable--header {
        color: #55647f;
        text-style: none;
    }
    #search {
        margin: 0 1;
    }
    #dirinput {
        margin: 0 1;
    }
    Footer {
        background: #0a0c12;
    }
    """

    BINDINGS = [
        ("q", "quit", "quit"),
        ("/", "search", "search"),
        ("1", "filter_all", "all"),
        ("2", "filter_claude", "claude"),
        ("3", "filter_codex", "codex"),
        ("4", "filter_opencode", "opencode"),
        ("c", "new_claude", "new claude"),
        ("x", "new_codex", "new codex"),
        ("o", "new_opencode", "new opencode"),
        ("t", "new_terminal", "terminal"),
        Binding("tab", "switch_plan", "switch plan", priority=True),
        ("C", "clear", "clear history"),
        ("u", "undo", "undo"),
        ("r", "refresh", "refresh"),
        Binding("escape", "escape", show=False),
    ]

    def __init__(self, cmd_file: str | None = None, limit: int = 300):
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
        # unified selection chain: launch(chips) -> picker(dirs) -> active -> history
        self.zone = "launch"
        self.launch_idx = 0  # starts on claude
        self.active_idx = 0
        self.dirs: list[tuple[str, int, datetime | None]] = []  # directory picker rows
        self.picker_idx = 0  # highlighted directory; enter opens exactly this one
        self.picker_opened: list[str] = []  # dirs opened this run, for footer feedback
        self._spin = 0  # spinner frame for working agents
        self.active_claude: str | None = None  # active claude plan label (multi-account)
        self.cleared_at = _load_cleared()

    def compose(self) -> ComposeResult:
        yield Static(id="banner")
        yield Static("loading usage…", id="usage", classes="panel")
        yield _ActivePanel("scanning processes…", id="active", classes="panel")
        yield Static(id="launch")
        yield Static(id="picker")
        yield _SessionsTable(id="sessions", cursor_type="none", zebra_stripes=True)
        yield Input(placeholder="search titles, prompts, projects…  (esc to close)", id="search")
        yield Input(placeholder="directory path to open…  (~ ok · enter opens · esc cancels)", id="dirinput")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sessions", DataTable)
        table.add_columns("tool", "title", "project", "active", "msgs", "tokens")
        table.can_focus = False
        search = self.query_one("#search", Input)
        search.display = False
        search.can_focus = False  # only focusable while the search box is open
        dirinput = self.query_one("#dirinput", Input)
        dirinput.display = False
        dirinput.can_focus = False  # only focusable while the path prompt is open
        self.set_focus(None)  # keys flow to on_key / bindings, not an auto-focused widget
        self.query_one("#active", Static).border_title = "active now"
        self.query_one("#usage", Static).border_title = "subscriptions"
        self._render_banner()
        self.dirs = self._recent_dirs()
        self._render_launch()
        self._render_picker()
        self.load_sessions()
        self.load_usage()
        self.load_running()
        # live activity polls fast; usage is slow (API); the spinner animates
        # locally between polls so "working" rows feel alive.
        self.set_interval(ACTIVE_POLL_SECONDS, self.load_running)
        self.set_interval(USAGE_POLL_SECONDS, self.load_usage)
        self.set_interval(0.12, self._animate_active)

    def _animate_active(self) -> None:
        if any(a.state == "working" for a in self.running):
            self._spin += 1
            self._render_active()

    # ------------------------------------------------------------- chrome

    def _render_banner(self) -> None:
        banner = self.query_one("#banner", Static)
        art = _banner_art(self.size.width - BANNER_CHROME)
        banner.update(f"[bold]{escape(art)}[/]" if art else f"[bold]◆ {BANNER_TEXT}[/]")

    def _render_launch(self) -> None:
        chips = []
        for i, tool in enumerate(LAUNCH_TOOLS):
            if tool == "terminal":
                color, glyph = "#55647f", "$"
            else:
                color, glyph = TOOL_COLORS[tool], "●"
            label = f"[{color}]{glyph}[/{color}] {tool}"
            if self.zone == "launch" and i == self.launch_idx:
                chips.append(f"[reverse bold] {label} [/]")
            else:
                chips.append(f"  {label} ")
        hint = "[dim]← → select · ↓ choose directories · enter opens[/]"
        self.query_one("#launch", Static).update(
            "[bold]new session[/]   " + "".join(chips) + "   " + hint
        )

    def _render_picker(self) -> None:
        tool = LAUNCH_TOOLS[self.launch_idx]
        rows = []
        for i, (path, count, last) in enumerate(self.dirs):
            disp = path.replace(str(Path.home()), "~")
            meta = "current" if i == 0 else rel_time(last) if last else ""
            if count:
                meta = f"{meta} · {count} session{'s' if count != 1 else ''}" if meta else f"{count} sessions"
            row = f"{escape(_truncate(disp, 50))}   [dim]{meta}[/]"
            if self.zone == "picker" and i == self.picker_idx:
                rows.append(f"[reverse] ▸ {row} [/]")
            else:
                rows.append(f"   {row}")
        other = "[#7aa2f7]+[/] open another directory…"
        if self.zone == "picker" and self.picker_idx == len(self.dirs):
            rows.append(f"[reverse] ▸ {other} [/]")
        else:
            rows.append(f"   {other}")
        # no checkboxes: enter opens the highlighted dir and leaves the picker up,
        # so several tabs is just several presses. the footer echoes what opened.
        verb = "open" if tool != "terminal" else "shell in"
        footer = f"[dim]launch {tool} in[/]   [#7aa2f7]⏎ {verb}[/]"
        if self.picker_opened:
            names = ", ".join(Path(p).name or p for p in self.picker_opened)
            footer += f"   [dim]opened: {escape(_truncate(names, 46))}[/]"
        rows.append(footer)
        self.query_one("#picker", Static).update("\n".join(rows))

    # ------------------------------------------------------------- selection chain

    def _set_zone(self, zone: str) -> None:
        self.zone = zone
        table = self.query_one("#sessions", DataTable)
        table.cursor_type = "row" if zone == "history" else "none"
        if zone == "history" and self.filtered:
            table.move_cursor(row=min(table.cursor_row or 0, len(self.filtered) - 1))
        self.active_idx = min(self.active_idx, max(0, len(self.running) - 1))
        self._render_launch()
        self._render_picker()
        self._render_active()

    def _enter_history_top(self, table) -> None:
        self._set_zone("history")
        if self.filtered:
            table.move_cursor(row=0)

    def on_key(self, event) -> None:
        if len(self.screen_stack) > 1:
            return  # a modal owns the keyboard
        if self.query_one("#search", Input).display:
            return  # the search box owns the keyboard while it's open
        if self.query_one("#dirinput", Input).display:
            return  # the path prompt owns the keyboard while it's open
        key = event.key
        if key not in ("enter", "left", "right", "up", "down"):
            return
        event.stop()  # keep the DataTable's own arrow-key bindings from also firing
        table = self.query_one("#sessions", DataTable)

        if key == "enter":
            if self.zone == "picker" and self.picker_idx == len(self.dirs):
                self._open_dir_input()  # the "open another directory…" row
            elif self.zone in ("launch", "picker"):
                self._launch(LAUNCH_TOOLS[self.launch_idx])
            elif self.zone == "active":
                self._enter_active()
            else:
                self.action_resume()
        elif key in ("left", "right") and self.zone in ("launch", "picker"):
            step = -1 if key == "left" else 1
            self.launch_idx = (self.launch_idx + step) % len(LAUNCH_TOOLS)
            self._render_launch()
            self._render_picker()  # footer verb tracks the selected tool
        elif key == "up":
            if self.zone == "launch":
                pass  # top of the chain
            elif self.zone == "picker":
                if self.picker_idx <= 0:
                    self._set_zone("launch")
                else:
                    self.picker_idx -= 1
                    self._render_picker()
            elif self.zone == "active":
                if self.active_idx <= 0:
                    self.picker_idx = len(self.dirs)  # land on the "other directory" row
                    self._set_zone("picker")
                else:
                    self.active_idx -= 1
                    self._render_active()
            elif self.zone == "history":
                row = table.cursor_row or 0
                if row <= 0:
                    if self.running:
                        self.active_idx = len(self.running) - 1
                        self._set_zone("active")
                    else:
                        self.picker_idx = len(self.dirs)  # land on the "other directory" row
                        self._set_zone("picker")
                else:
                    table.move_cursor(row=row - 1)
        elif key == "down":
            if self.zone == "launch":
                self.picker_idx = 0
                self._set_zone("picker")
            elif self.zone == "picker":
                if self.picker_idx < len(self.dirs):  # step through dirs + the "other" row
                    self.picker_idx += 1
                    self._render_picker()
                elif self.running:
                    self.active_idx = 0
                    self._set_zone("active")
                else:
                    self._enter_history_top(table)
            elif self.zone == "active":
                if self.active_idx >= len(self.running) - 1:
                    self._enter_history_top(table)
                else:
                    self.active_idx += 1
                    self._render_active()
            elif self.zone == "history":
                row = table.cursor_row or 0
                table.move_cursor(row=min(row + 1, len(self.filtered) - 1))

    # ------------------------------------------------------------- active now

    @work(thread=True, exclusive=True, group="running")
    def load_running(self) -> None:
        agents = enrich(running_agents())
        self.call_from_thread(self._on_running, agents)

    def _on_running(self, agents: list[RunningAgent]) -> None:
        self.running = agents
        self.active_idx = min(self.active_idx, max(0, len(agents) - 1))
        self._resolve_agent_titles()  # cached: _render_active also runs on the spinner tick
        self._render_active()

    def _render_active(self) -> None:
        panel = self.query_one("#active", Static)
        panel.border_title = f"active now · {len(self.running)}"
        if not self.running:
            panel.update("[dim]no agent sessions running right now[/]")
            return
        working = sum(1 for a in self.running if a.state == "working")
        if working:
            panel.border_title = f"active now · {len(self.running)} · {working} working"
        frame = _SPINNER[self._spin % len(_SPINNER)]
        lines = []
        for i, agent in enumerate(self.running[:MAX_ACTIVE_ROWS]):
            title = self._title_for(agent)
            color = TOOL_COLORS[agent.tool]
            marker = _status_marker(agent.state, frame, color)
            head = f"{marker} [{color}]{agent.tool}[/{color}]"
            pad = " " * (10 - len(agent.tool))
            cwd_display = (agent.cwd or "?").replace(str(Path.home()), "~")
            row = (
                f"{head}{pad}{escape(_truncate(title, 42))}   "
                f"[dim]{escape(_truncate(cwd_display, 32))} · {agent.tty} · up {agent.elapsed}[/]"
            )
            if self.zone == "active" and i == self.active_idx:
                lines.append(f"[reverse bold] {row} [/]")
            else:
                lines.append(f"  {row}")
            detail = _activity_detail(agent)
            if detail:
                lines.append(f"             {detail}")
        if len(self.running) > MAX_ACTIVE_ROWS:
            lines.append(f"[dim]…and {len(self.running) - MAX_ACTIVE_ROWS} more[/]")
        lines.append("[dim]enter or click jumps to the tab[/]")
        panel.update("\n".join(lines))

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

    def _enter_active(self) -> None:
        if not self.running:
            return
        self._focus_agent(self.running[self.active_idx])

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

    def _agent_at_line(self, line: int) -> int | None:
        """Map a content row in the #active panel to a running-agent index.

        Each agent takes one row, plus a second when it has a detail line
        (current action / live tokens); clicking either selects that agent.
        """
        if line < 0:
            return None
        cursor = 0
        for i, agent in enumerate(self.running[:MAX_ACTIVE_ROWS]):
            cursor += 1
            if line < cursor:
                return i
            if _activity_detail(agent):
                cursor += 1
                if line < cursor:
                    return i
        return None

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
        self._render_usage()

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
        # the third column is a model-scoped weekly limit (e.g. "fable"), shown
        # only when some plan actually reports one
        extra = next(
            (w.label for u in self.usages for w in u.windows if w.label not in ("5h", "7d")),
            None,
        )
        # room left for a stale marker after the bars; the grid comes first, so a
        # narrow terminal drops the marker rather than wrapping the row
        cells = 3 if extra else 2
        marker_room = (width or 200) - (2 + USAGE_TOOL_W + USAGE_PLAN_W
                                        + cells * (USAGE_CELL_W + len(USAGE_GAP)))
        cols = (
            _pad_visible("5h", 2, USAGE_CELL_W + len(USAGE_GAP))
            + (_pad_visible("7d", 2, USAGE_CELL_W + len(USAGE_GAP)) + extra if extra else "7d")
        )
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
                lines.append(f"{prefix}{empty}{USAGE_GAP}{spend_cell}")
                continue
            windows = {w.label: w for w in usage.windows}
            row = f"{prefix}{_window_cell(windows.get('5h'))}{USAGE_GAP}{_window_cell(windows.get('7d'))}"
            if extra and extra in windows:  # this plan has the scoped limit; add its cell
                row += f"{USAGE_GAP}{_window_cell(windows[extra])}"
            if usage.stale and marker_room >= 6:  # cached bars: say so after them
                mark = _truncate(usage.stale, marker_room)
                row += f"{USAGE_GAP}[dim]{escape(mark)}[/]"
            lines.append(row)
        multi = sum(1 for u in self.usages if u.tool == "claude" and u.label) > 1
        if multi:
            lines.append("[dim]↹ tab switches the active claude plan[/]")
        return "\n".join(lines)

    def _render_usage(self) -> None:
        panel = self.query_one("#usage", Static)
        panel.update(self._usage_text(panel.size.width or None))

    # ------------------------------------------------------------- history

    @work(thread=True)
    def load_sessions(self) -> None:
        sessions = collect_all(limit=self.limit)
        self.call_from_thread(self._on_loaded, sessions)

    def _on_loaded(self, sessions: list[Session]) -> None:
        self.sessions = sessions
        self.dirs = self._recent_dirs()
        self.picker_idx = min(self.picker_idx, len(self.dirs))  # len(dirs) == the "other" row
        self._render_picker()
        self._populate()
        self.load_running()

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

    def _populate(self) -> None:
        table = self.query_one("#sessions", DataTable)
        table.clear()
        self.filtered = [s for s in self.sessions if self._matches(s)]
        # budget the column widths from the actual terminal width:
        # tool 10, project 12, active 9, msgs 6, tokens 8, cell padding 12
        width = table.size.width or 80
        title_w = max(20, width - 57)
        for i, session in enumerate(self.filtered):
            chip = Text("● ", style=TOOL_COLORS[session.tool]) + Text(session.tool, style=TOOL_COLORS[session.tool])
            table.add_row(
                chip,
                _truncate(session.title, title_w),
                _truncate(session.project_name, 12),
                rel_time(session.last_active),
                str(session.n_messages) if session.n_messages else "—",
                fmt_tokens(session.tokens),
                key=str(i),
            )
        state = "cleared" if self.cleared_at else self.tool_filter
        table.border_title = f"history ({state}) · {len(self.filtered)}"
        table.border_subtitle = "enter resumes · / searches · C clears"
        if self.filtered and self.zone == "history":
            table.move_cursor(row=min(table.cursor_row or 0, len(self.filtered) - 1))

    # ------------------------------------------------------------- events

    @on(DataTable.RowSelected)
    def _on_selected(self, event: DataTable.RowSelected) -> None:
        self.action_resume()

    @on(Input.Changed, "#search")
    def _on_search_changed(self, event: Input.Changed) -> None:
        self.query = event.value
        self._populate()

    @on(Input.Submitted, "#search")
    def _on_search_submitted(self) -> None:
        self._close_search()

    # ------------------------------------------------------------- actions

    def action_resume(self) -> None:
        table = self.query_one("#sessions", DataTable)
        if not self.filtered or table.cursor_row is None:
            return
        row = min(table.cursor_row, len(self.filtered) - 1)
        self.selected = self.filtered[row]
        if self.cmd_file:
            with open(self.cmd_file, "w") as fh:
                fh.write(resume_command(self.selected) + "\n")
        self.exit()

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
        self._render_picker()  # footer echoes what has been opened

    def _open_tab(self, tool: str, cwd: str, suffix: str = "", env: dict | None = None) -> bool:
        try:
            stem = _write_tab_config(tool, cwd, _warp_configs_dir(), suffix=suffix, env=env)
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

    def action_clear(self) -> None:
        self.cleared_at = datetime.now(timezone.utc)
        _save_cleared(self.cleared_at)
        self._populate()
        self.notify("history cleared — press u to undo", timeout=3)

    def action_undo(self) -> None:
        if not self.cleared_at:
            return
        self.cleared_at = None
        _save_cleared(None)
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

    def action_refresh(self) -> None:
        self.load_sessions()
        self.load_usage(force=True)

    def on_resize(self) -> None:
        self._render_banner()
        if self.usages:
            self._render_usage()  # the stale marker is budgeted from the width
        if self.sessions:
            self._populate()
