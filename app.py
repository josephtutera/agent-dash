"""Textual TUI for adash: subscription usage + active agents + session history."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import DEVNULL, Popen

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Input, Static

from activity import enrich
from agents import RunningAgent, running_agents
from collectors import collect_all
from models import TOOL_COLORS, Session, fmt_tokens, rel_time, resume_command
from usage import ToolUsage, UsageWindow, claude_profiles, collect_usage

BAR_WIDTH = 24
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
USAGE_POLL_SECONDS = 45.0  # usage refresh (Claude hits an API, so keep it slow)


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


def _bar(pct: float | None) -> str:
    if pct is None:
        return f"[dim]{'░' * BAR_WIDTH} n/a[/]"
    filled = round(BAR_WIDTH * min(pct, 100.0) / 100.0)
    # instrument palette: calm blue until it runs warm, then amber, then red
    color = "#7aa2f7" if pct < 60 else "#ffb454" if pct < 85 else "#ff5c57"
    return f"[{color}]{'█' * filled}[/{color}][#1b2233]{'░' * (BAR_WIDTH - filled)}[/] {pct:.0f}%"


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


def _fmt_reset(dt: datetime | None) -> str:
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    local = dt.astimezone()
    if dt - now < timedelta(hours=24):
        return local.strftime("%-I:%M%p").lower()
    return local.strftime("%b %d")


_TAB_COLORS = {"claude": "magenta", "codex": "green", "opencode": "blue", "terminal": "blue"}
# None means "open a plain shell in the directory" — no agent command is run
_TAB_COMMANDS = {"claude": "claude", "codex": "codex", "opencode": "opencode", "terminal": None}


def _warp_configs_dir() -> Path:
    return Path.home() / ".warp" / "tab_configs"


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
    #spacer {
        height: 1fr;
    }
    #sessions {
        height: 12;
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
        # unified selection chain: launch(chips) -> picker(dirs) -> active -> history
        self.zone = "launch"
        self.launch_idx = 0  # starts on claude
        self.active_idx = 0
        self.dirs: list[tuple[str, int, datetime | None]] = []  # directory picker rows
        self.picker_idx = 0  # highlighted directory
        self.picker_selected: set[int] = {0}  # chosen directories (cwd preselected)
        self._spin = 0  # spinner frame for working agents
        self.active_claude: str | None = None  # active claude plan label (multi-account)
        self.cleared_at = _load_cleared()

    def compose(self) -> ComposeResult:
        yield Static(id="banner")
        yield Static("loading usage…", id="usage", classes="panel")
        yield Static("scanning processes…", id="active", classes="panel")
        yield Static(id="launch")
        yield Static(id="picker")
        yield Static(id="spacer")
        yield _SessionsTable(id="sessions", cursor_type="none", zebra_stripes=True)
        yield Input(placeholder="search titles, prompts, projects…  (esc to close)", id="search")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sessions", DataTable)
        table.add_columns("tool", "title", "project", "active", "msgs", "tokens")
        table.can_focus = False
        search = self.query_one("#search", Input)
        search.display = False
        search.can_focus = False  # only focusable while the search box is open
        self.set_focus(None)  # keys flow to on_key / bindings, not an auto-focused widget
        self.query_one("#active", Static).border_title = "active now"
        self.query_one("#usage", Static).border_title = "subscriptions"
        self._render_banner()
        self.dirs = self._recent_dirs()
        self._render_launch()
        self._render_picker()
        self._fit_history()
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
            box = "[#7aa2f7]◼[/]" if i in self.picker_selected else "[#55647f]◻[/]"
            disp = path.replace(str(Path.home()), "~")
            meta = "current" if i == 0 else rel_time(last) if last else ""
            if count:
                meta = f"{meta} · {count} session{'s' if count != 1 else ''}" if meta else f"{count} sessions"
            row = f"{box} {escape(_truncate(disp, 50))}   [dim]{meta}[/]"
            if self.zone == "picker" and i == self.picker_idx:
                rows.append(f"[reverse] {row} [/]")
            else:
                rows.append(f"  {row}")
        n = len(self.picker_selected)
        verb = "open" if tool != "terminal" else "shell in"
        rows.append(f"[dim]launch {tool} in[/]   [#7aa2f7]⏎ {verb} {n} tab{'s' if n != 1 else ''}[/]   [dim]space toggles[/]")
        self.query_one("#picker", Static).update("\n".join(rows))

    def _fit_history(self) -> None:
        # the picker now sits above history too, so leave it more room up top
        picker_rows = len(self.dirs) + 1
        self.query_one("#sessions", DataTable).styles.height = max(5, min(10, self.size.height - 34 - picker_rows))

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
        key = event.key
        if key not in ("enter", "left", "right", "up", "down", "space"):
            return
        event.stop()  # keep the DataTable's own arrow-key bindings from also firing
        table = self.query_one("#sessions", DataTable)

        if key == "enter":
            if self.zone in ("launch", "picker"):
                self._launch(LAUNCH_TOOLS[self.launch_idx])
            elif self.zone == "active":
                self._enter_active()
            else:
                self.action_resume()
        elif key == "space" and self.zone == "picker":
            self.picker_selected.symmetric_difference_update({self.picker_idx})
            self._render_picker()
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
                    self.picker_idx = max(0, len(self.dirs) - 1)
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
                        self.picker_idx = max(0, len(self.dirs) - 1)
                        self._set_zone("picker")
                else:
                    table.move_cursor(row=row - 1)
        elif key == "down":
            if self.zone == "launch":
                self.picker_idx = 0
                self._set_zone("picker")
            elif self.zone == "picker":
                if self.picker_idx < len(self.dirs) - 1:
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
        panel.update("\n".join(lines))

    def _title_for(self, agent: RunningAgent) -> str:
        """Match a running process to its session via tool + working directory."""
        candidates = [
            s for s in self.sessions
            if s.tool == agent.tool and agent.cwd and s.project_dir == agent.cwd
        ]
        if candidates:
            return candidates[0].title
        return Path(agent.cwd).name or "session"

    def _enter_active(self) -> None:
        if not self.running:
            return
        agent = self.running[self.active_idx]
        self.notify(f"{agent.tool} is already running in another tab ({agent.tty})", timeout=3)

    # ------------------------------------------------------------- usage

    @work(thread=True)
    def load_usage(self) -> None:
        usages, fetched_at = collect_usage(self.active_claude)
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

    def _render_usage(self) -> None:
        lines = []
        for usage in self.usages:
            color = TOOL_COLORS[usage.tool]
            if usage.label:  # multi-account claude: active/inactive marker + label
                dot = "[#7aa2f7]◉[/]" if usage.active else "[#55647f]○[/]"
                head = f"{dot} [{color}]{usage.tool}[/{color}]"
                plan_text = f"{usage.plan} · {usage.label}" if usage.plan else usage.label
            else:
                head = f"[{color}]● {usage.tool}[/{color}]"
                plan_text = usage.plan
            pad = " " * (10 - len(usage.tool))
            plan = f"[dim]{escape(plan_text)}[/]" if plan_text else ""
            if usage.error:
                lines.append(f"{head}{pad}{plan} [dim]{escape(usage.error)}[/]")
                continue
            parts = [f"{head}{pad}{plan}"]
            for window in usage.windows:
                reset = _fmt_reset(window.resets_at)
                reset_text = f" [dim]resets {reset}[/]" if reset else ""
                parts.append(f"[dim]{window.label}[/] {_bar(window.pct)}{reset_text}")
            if usage.note:
                parts.append(f"[dim]{escape(usage.note)}[/]")
            lines.append("  ".join(parts))
        multi = sum(1 for u in self.usages if u.tool == "claude" and u.label) > 1
        if multi:
            lines.append("[dim]↹ tab switches the active claude plan[/]")
        self.query_one("#usage", Static).update("\n".join(lines))

    # ------------------------------------------------------------- history

    @work(thread=True)
    def load_sessions(self) -> None:
        sessions = collect_all(limit=self.limit)
        self.call_from_thread(self._on_loaded, sessions)

    def _on_loaded(self, sessions: list[Session]) -> None:
        self.sessions = sessions
        self.dirs = self._recent_dirs()
        self.picker_idx = min(self.picker_idx, max(0, len(self.dirs) - 1))
        if not self.picker_selected:
            self.picker_selected = {0}
        self._render_picker()
        self._fit_history()
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
            if not d:
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
        chosen = [self.dirs[i][0] for i in sorted(self.picker_selected) if i < len(self.dirs)]
        return chosen or [os.getcwd()]

    def _launch(self, tool: str) -> None:
        """Open a Warp tab per selected directory running `tool` (a bare shell
        for `terminal`). Claude uses the active plan's CLAUDE_CONFIG_DIR."""
        dirs = self._selected_dirs()
        env: dict = {}
        plan = ""
        if tool == "claude":
            profile = self._active_claude_profile()
            if not profile.default:
                env = {"CLAUDE_CONFIG_DIR": str(profile.config_dir)}
                plan = f" ({profile.label})"
        failed = False
        for d in dirs:
            failed = not self._open_tab(tool, d, suffix=_dir_slug(d), env=env) or failed
        if not failed:
            self.notify(f"opening {tool}{plan} in {len(dirs)} tab{'s' if len(dirs) != 1 else ''}", timeout=2)

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

    def action_escape(self) -> None:
        if self.query_one("#search", Input).display:
            self._close_search()

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
        self.load_usage()

    def on_resize(self) -> None:
        self._render_banner()
        self._fit_history()
        if self.sessions:
            self._populate()
