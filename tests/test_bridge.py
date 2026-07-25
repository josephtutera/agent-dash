"""Tests for the Bridge presentation layer.

These lock down the two rules the redesign rests on, because both are the kind
of thing a later change quietly undoes: colour must track agent state rather
than which tool it is, and the detail pane must always end with a sentence
naming what Enter does.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent.parent))

import bridge
from agents import RunningAgent
from bridge import DARK, LIGHT, THEMES, Theme
from models import Session

NOW = datetime.now(timezone.utc)
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")

ROLES = (
    "ground", "raised", "selected", "rule",
    "text", "text_soft", "text_dim",
    "accent", "running", "waiting",
)


def plain(markup: str) -> str:
    """Markup with the tags removed, so assertions read like the screen does."""
    return Text.from_markup(markup).plain


def _session(**kw) -> Session:
    fields = dict(
        tool="claude",
        id="9f2c41a8",
        title="Agent Dash HUD Redesign Concepts",
        project_dir=str(Path.home() / "Repos" / "agent-dash"),
        last_active=NOW - timedelta(days=1),
        n_messages=365,
        tokens=98_500_000,
    )
    fields.update(kw)
    return Session(**fields)


def _agent(**kw) -> RunningAgent:
    fields = dict(
        tool="codex",
        pid=4242,
        tty="ttys003",
        elapsed="1m",
        cwd=str(Path.home() / "Repos" / "web-app"),
        title="Comment Quality and Artifact Detection Gate",
        state="working",
        label="running tests",
        tokens=102_700_000,
    )
    fields.update(kw)
    return RunningAgent(**fields)


# ------------------------------------------------------------------- the theme

@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=lambda t: t.name)
def test_every_role_is_a_real_hex(theme: Theme):
    for role in ROLES:
        value = getattr(theme, role)
        assert HEX.match(value), f"{theme.name}.{role} is not a hex colour: {value}"


def test_light_and_dark_share_no_values():
    """A theme pair that accidentally reuses a value is the classic way dark
    mode ends up unreadable, so require every role to have been retuned."""
    for role in ROLES:
        assert getattr(LIGHT, role) != getattr(DARK, role), f"{role} was not retuned for dark"


def test_themes_registry_matches_the_dataclasses():
    assert THEMES == {"light": LIGHT, "dark": DARK}
    assert bridge.DEFAULT_THEME in THEMES


def test_css_variables_cover_every_role():
    for theme in (LIGHT, DARK):
        variables = bridge.css_variables(theme)
        assert len(variables) == len(ROLES)
        assert set(variables.values()) == {getattr(theme, r) for r in ROLES}
        assert all(name.startswith("ad-") for name in variables)


# ------------------------------------------------- colour means state, not tool

@pytest.mark.parametrize("tool", ["claude", "codex", "opencode", "gemini"])
def test_state_colour_ignores_the_tool(tool: str):
    """The whole point of the redesign: two agents in the same state look the
    same whatever CLI they are."""
    working = bridge.state_colour("working", DARK)
    for other in ("claude", "codex", "opencode", "gemini"):
        assert bridge.state_colour("working", DARK) == working
    assert bridge.agent_row(_agent(tool=tool, state="working"), "x", theme=DARK).marker == \
        bridge.agent_row(_agent(tool="claude", state="working"), "x", theme=DARK).marker


def test_state_colours_are_distinct_and_correct():
    assert bridge.state_colour("working", DARK) == DARK.running
    assert bridge.state_colour("waiting", DARK) == DARK.waiting
    assert bridge.state_colour("idle", DARK) == DARK.text_dim
    # an unresolved state reads as quiet, not as an error
    assert bridge.state_colour("unknown", DARK) == DARK.text_dim


def test_waiting_is_the_only_loud_state():
    """Amber is reserved for "this one wants you"; nothing else may use it."""
    loud = DARK.waiting
    assert bridge.state_colour("waiting", DARK) == loud
    for state in ("working", "idle", "unknown"):
        assert bridge.state_colour(state, DARK) != loud


# --------------------------------------------------------------------- helpers

def test_truncate_only_clips_when_it_has_to():
    assert bridge.truncate("short", 10) == "short"
    assert bridge.truncate("x" * 10, 10) == "x" * 10
    assert bridge.truncate("x" * 11, 10) == "x" * 9 + "…"
    assert len(bridge.truncate("x" * 99, 10)) == 10


def test_compact_stamp_fits_the_four_cell_column():
    assert bridge.compact_stamp("just now") == "now"
    assert bridge.compact_stamp("12m ago") == "12m"
    assert bridge.compact_stamp("20h ago") == "20h"
    assert bridge.compact_stamp("2d ago") == "2d"
    assert len(bridge.compact_stamp("just now")) <= 4


# ------------------------------------------------------------------- list rows

def test_agent_row_surfaces_waiting_in_the_meta_line():
    row = bridge.agent_row(_agent(state="waiting", label=""), "AI Next Evals", theme=DARK)
    assert "waiting on you" in row.meta
    assert plain(row.marker) == "◐"


def test_agent_row_leads_with_the_tool_then_the_project():
    row = bridge.agent_row(_agent(), "Comment Quality", theme=DARK)
    assert row.meta.startswith("codex · web-app")
    assert "running tests" in row.meta


def test_session_row_has_no_state_marker():
    """The empty marker slot is what separates finished sessions from running
    ones without needing a second heading."""
    row = bridge.session_row(_session())
    assert row.marker == ""
    assert "365 msgs" in row.meta


def test_list_titles_are_truncated_to_the_lane_width():
    row = bridge.session_row(_session(title="x" * 200))
    assert len(row.title) == bridge.LIST_TITLE_W


def test_render_row_right_aligns_the_stamp_and_bars_only_the_selection():
    unselected = bridge.render_row(bridge.session_row(_session()), DARK, 54)
    selected = bridge.render_row(bridge.session_row(_session(), selected=True), DARK, 54)
    assert bridge.SELECTION_BAR not in plain(unselected)
    assert plain(selected).startswith(bridge.SELECTION_BAR)
    head = plain(selected).splitlines()[0]
    assert head.rstrip() == head.rstrip(" ")  # stamp is the last thing on the line
    assert head.endswith("1d")


def test_render_row_puts_meta_on_a_second_line():
    rendered = bridge.render_row(bridge.session_row(_session()), DARK, 54)
    lines = plain(rendered).splitlines()
    assert len(lines) == 2
    assert "claude · agent-dash" in lines[1]


# ----------------------------------------------------------------- detail pane

def test_detail_running_names_the_state_and_the_tab():
    # the eyebrow fakes letter-spacing by padding the characters, so compare
    # against the de-spaced text rather than the literal line
    text = plain(bridge.detail_running(_agent(state="waiting"), _session(), DARK))
    squeezed = text.replace(" ", "")
    assert "WAITINGONYOU" in squeezed
    assert "TTYS003" in squeezed.upper()


def test_detail_session_shows_the_literal_resume_command():
    """The one thing history is actually for. If this regresses the pane is
    decorative."""
    session = _session(tool="claude", id="9f2c41a8-6b0e")
    text = plain(bridge.detail_session(session, DARK))
    assert "claude --resume 9f2c41a8-6b0e" in text


@pytest.mark.parametrize("tool", ["claude", "codex", "opencode", "gemini"])
def test_detail_session_resume_command_matches_the_tool(tool: str):
    text = plain(bridge.detail_session(_session(tool=tool, id="abc"), DARK))
    assert tool in text
    assert "abc" in text


def test_detail_panes_carry_the_project_directory():
    assert "~/Repos/web-app" in plain(bridge.detail_running(_agent(), None, DARK))
    assert "~/Repos/agent-dash" in plain(bridge.detail_session(_session(), DARK))


def test_action_bar_states_the_key_and_the_sentence():
    bar = bridge.action_bar(DARK, "↵", "jump to this tab in Warp", "ttys003")
    assert "↵" in plain(bar)
    assert "jump to this tab in Warp" in plain(bar)
    assert DARK.accent in bar  # it is the filled cobalt strip, not plain text


# -------------------------------------------------------------------- launcher

def _dirs():
    home = str(Path.home())
    return [
        (home, 37, None),
        (f"{home}/Repos/ai-next", 7, NOW - timedelta(seconds=5)),
        (f"{home}/Repos/web-app", 44, NOW - timedelta(minutes=1)),
    ]


TOOLS = ("claude", "codex", "opencode", "gemini", "terminal")


def test_launcher_offers_every_tool_the_old_picker_did():
    text = plain(bridge.launcher(DARK, TOOLS, 0, _dirs(), 0, []))
    for tool in TOOLS:
        assert tool in text


def test_launcher_pins_the_current_directory_first():
    text = plain(bridge.launcher(DARK, TOOLS, 0, _dirs(), 0, []))
    first_dir_line = [ln for ln in text.splitlines() if "37 sessions" in ln][0]
    assert "current" in first_dir_line


def test_launcher_marks_directories_already_opened_this_run():
    home = str(Path.home())
    text = plain(bridge.launcher(DARK, TOOLS, 0, _dirs(), 1, [f"{home}/Repos/web-app"]))
    web_app_line = [ln for ln in text.splitlines() if "web-app" in ln][0]
    assert "✓ opened" in web_app_line
    assert "opened this run: web-app" in text


def test_launcher_action_names_the_tool_and_the_target_directory():
    text = plain(bridge.launcher(DARK, TOOLS, 1, _dirs(), 1, []))
    assert "open codex in ~/Repos/ai-next" in text


def test_launcher_says_shell_for_the_terminal_tool():
    """`terminal` runs no agent, so "open terminal in X" would be a lie."""
    text = plain(bridge.launcher(DARK, TOOLS, 4, _dirs(), 0, []))
    assert "open a shell in terminal" in text


def test_launcher_keeps_the_typed_path_row():
    text = plain(bridge.launcher(DARK, TOOLS, 0, _dirs(), 0, []))
    assert "+ type a path…" in text


def test_launcher_shows_the_claude_plan_and_its_switch_hint():
    text = plain(
        bridge.launcher(DARK, TOOLS, 0, _dirs(), 0, [],
                        plan_label="Team · default",
                        plan_hint="tab switches to personal")
    )
    assert "running as Team · default" in text
    assert "tab switches to personal" in text


def test_launcher_selection_bar_follows_the_directory_cursor():
    rendered = bridge.launcher(DARK, TOOLS, 0, _dirs(), 2, [])
    lines = plain(rendered).splitlines()
    barred = [ln for ln in lines if ln.startswith(bridge.SELECTION_BAR)]
    assert len(barred) == 1
    assert "web-app" in barred[0]


def test_launcher_cursor_past_the_last_directory_lands_on_the_typed_path_row():
    rendered = bridge.launcher(DARK, TOOLS, 0, _dirs(), 3, [])
    barred = [ln for ln in plain(rendered).splitlines() if ln.startswith(bridge.SELECTION_BAR)]
    assert len(barred) == 1
    assert "type a path" in barred[0]


# ---------------------------------------------------------------------- search

def test_search_header_reports_what_is_hidden():
    text = plain(bridge.search_header(DARK, "gate", 3, 37))
    assert "gate" in text
    assert "3 of 37 sessions" in text


def test_match_block_quotes_up_to_three_snippets():
    block = bridge.match_block(DARK, ["one", "two", "three", "four"], "4 messages")
    text = plain(block)
    assert "four" not in text
    assert text.count("▎") == 3


def test_match_block_is_empty_when_nothing_matched():
    assert bridge.match_block(DARK, [], "nothing") == ""
