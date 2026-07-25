"""Tests for the Bridge layout's app-level behaviour.

`test_bridge.py` covers what the panes say; this covers how the shell behaves —
the decisions that are easy to regress because they only show up in a running
app: the cursor rules, the narrow-terminal fallback, and the theme it starts in.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_module
import bridge
from agents import RunningAgent
from app import AdashApp
from models import Session
from usage import ToolUsage, UsageWindow

NOW = datetime.now(timezone.utc)


def _sessions() -> list[Session]:
    return [
        Session("claude", "abc", "First session", "/tmp/one", NOW - timedelta(minutes=5),
                n_messages=4, tokens=1000, first_prompt="do the first thing"),
        Session("codex", "xyz", "Second session", "/tmp/two", NOW - timedelta(hours=3),
                n_messages=9, tokens=2000, first_prompt="do the second thing"),
    ]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Stub every source of truth so the app boots deterministically offline."""
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _sessions())
    monkeypatch.setattr(app_module, "collect_usage",
                        lambda *a, **k: ([ToolUsage(tool="claude", plan="Team",
                                                    windows=[UsageWindow("7d", 40.0)])], NOW))
    monkeypatch.setattr(app_module, "running_agents", lambda: [])
    monkeypatch.setattr(app_module, "enrich", lambda agents: agents)
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    return tmp_path


async def _ready(pilot, app) -> None:
    for _ in range(100):
        await pilot.pause(0.05)
        if app.entries:
            return


def _run(coro_factory):
    asyncio.run(coro_factory())


# ------------------------------------------------------------------ the cursor

def test_a_running_agent_is_not_also_listed_as_history(harness, monkeypatch):
    """The old screen showed live work twice, once in "active now" and again at
    the top of the table. One list means one row per piece of work."""
    monkeypatch.setattr(app_module, "running_agents", lambda: [
        RunningAgent(tool="claude", pid=1, tty="ttys001", elapsed="5m", cwd="/tmp/one",
                     session_id="abc", state="working"),
    ])

    async def run():
        app = AdashApp()
        async with app.run_test(size=(120, 32)) as pilot:
            await _ready(pilot, app)
            for _ in range(100):
                await pilot.pause(0.05)
                if app.running:
                    break
            ids = [e.session.id for e in app.entries if e.kind == "session"]
            assert "abc" not in ids  # it is the live row instead
            assert "xyz" in ids
            assert sum(1 for e in app.entries if e.kind == "agent") == 1

    _run(run)


def test_the_cursor_follows_the_top_until_you_move_it(harness, monkeypatch):
    """A live agent appearing a moment after boot should take the selection; a
    live agent appearing after you have chosen a row must not steal it."""
    agents: list = []
    monkeypatch.setattr(app_module, "running_agents", lambda: list(agents))

    async def run():
        app = AdashApp()
        async with app.run_test(size=(120, 32)) as pilot:
            await _ready(pilot, app)
            assert app.cursor == 0 and app.current.kind == "session"

            agents.append(RunningAgent(tool="codex", pid=7, tty="ttys009",
                                       elapsed="1m", cwd="/tmp/three", state="working"))
            for _ in range(100):
                await pilot.pause(0.05)
                if app.running:
                    break
            assert app.current.kind == "agent"  # untouched cursor follows the top

            await pilot.press("down")
            await pilot.pause(0.05)
            chosen = app.current.key
            app._rebuild()  # a poll lands
            await pilot.pause(0.05)
            assert app.current.key == chosen  # now it is yours and it stays put

    _run(run)


def test_the_cursor_stops_at_both_ends(harness):
    async def run():
        app = AdashApp()
        async with app.run_test(size=(120, 32)) as pilot:
            await _ready(pilot, app)
            for _ in range(10):
                await pilot.press("up")
            await pilot.pause(0.05)
            assert app.cursor == 0
            for _ in range(10):
                await pilot.press("down")
            await pilot.pause(0.05)
            assert app.cursor == len(app.entries) - 1

    _run(run)


# -------------------------------------------------------------- what Enter does

def test_the_footer_always_names_what_enter_will_do(harness):
    """The one promise the layout makes. In a narrow terminal the footer is the
    only place it survives, so it may never be blank or vague."""
    async def run():
        app = AdashApp()
        async with app.run_test(size=(120, 32)) as pilot:
            await _ready(pilot, app)
            assert app._action_words() == "resume this session"
            await pilot.press("n")
            await pilot.pause(0.05)
            assert "open claude in" in app._action_words()
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert app._action_words() == "resume this session"

    _run(run)


def test_escape_leaves_the_launcher(harness):
    async def run():
        app = AdashApp()
        async with app.run_test(size=(120, 32)) as pilot:
            await _ready(pilot, app)
            await pilot.press("n")
            await pilot.pause(0.05)
            assert app.mode == "launcher"
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert app.mode == "browse"

    _run(run)


def test_question_mark_opens_the_keymap_and_any_key_closes_it(harness):
    """Twelve keys came off the footer on the promise that ? would carry them."""
    async def run():
        app = AdashApp()
        async with app.run_test(size=(120, 32)) as pilot:
            await _ready(pilot, app)
            await pilot.press("question_mark")
            await pilot.pause(0.1)
            assert len(app.screen_stack) > 1
            await pilot.press("z")
            await pilot.pause(0.1)
            assert len(app.screen_stack) == 1

    _run(run)


def test_the_keymap_lists_every_binding_that_is_hidden_from_the_footer(harness):
    """A keymap that drifts from the bindings is worse than no keymap."""
    documented = {key for _, rows in AdashApp.KEYMAP_HELP for key, _ in rows}
    documented = {k for entry in documented for k in entry.replace("/", " ").split()}
    footer_keys = {"⏎", "n", "/", "?"}
    for binding in AdashApp.BINDINGS:
        key = binding.key
        if key in ("escape", "question_mark", "slash"):
            continue
        if key in ("1", "2", "3", "4", "5"):
            key = "1–5"
        assert key in documented or key in footer_keys, f"{key} is bound but undocumented"

    _run(lambda: asyncio.sleep(0))


# ----------------------------------------------------------------- narrow mode

def test_a_narrow_terminal_drops_the_detail_pane_instead_of_complaining(harness):
    async def run():
        app = AdashApp()
        async with app.run_test(size=(80, 32)) as pilot:
            await _ready(pilot, app)
            assert app.narrow
            assert app.screen.has_class("narrow")
            assert not app.query_one("#detail").display
            # the promise moves to the footer rather than disappearing
            assert app._action_words() == "resume this session"

    _run(run)


def test_a_wide_terminal_keeps_both_panes(harness):
    async def run():
        app = AdashApp()
        async with app.run_test(size=(140, 32)) as pilot:
            await _ready(pilot, app)
            assert not app.narrow
            assert not app.screen.has_class("narrow")
            assert app.query_one("#detail").display

    _run(run)


# --------------------------------------------------------------------- theming

def test_the_theme_toggle_is_remembered_across_launches(harness):
    async def run():
        first = AdashApp(theme_name="dark")
        async with first.run_test(size=(120, 32)) as pilot:
            await _ready(pilot, first)
            await pilot.press("T")
            await pilot.pause(0.1)
            assert first.theme_name == "light"
        assert json.loads((harness / "config.json").read_text())["theme"] == "light"
        # a fresh launch with no explicit theme picks the saved one back up
        assert app_module._load_theme_pref() == "light"

    _run(run)


def test_an_unreadable_config_falls_back_to_dark_without_complaining(harness):
    (harness / "config.json").write_text("{ this is not json")
    assert app_module._load_theme_pref() == bridge.DEFAULT_THEME


def test_a_bogus_theme_name_in_the_config_is_ignored(harness):
    (harness / "config.json").write_text(json.dumps({"theme": "chartreuse"}))
    assert app_module._load_theme_pref() == bridge.DEFAULT_THEME


def test_both_themes_resolve_every_css_variable_the_stylesheet_uses(harness):
    """A palette missing a role would render as an unstyled panel rather than
    an error, so check the stylesheet and the theme agree."""
    used = {name.strip("$;,()").removeprefix("$")
            for name in AdashApp.CSS.split() if name.startswith("$ad-")}
    assert used, "the stylesheet stopped using the palette at all"
    for theme_name in bridge.THEMES:
        provided = set(bridge.css_variables(bridge.THEMES[theme_name]))
        assert used <= provided, f"{theme_name} is missing {used - provided}"
