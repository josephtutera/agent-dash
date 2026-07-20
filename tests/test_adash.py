"""Tests for adash collectors, resume commands, and the TUI flow."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_module
import collectors
from app import AdashApp
from models import Session, resume_command
from textual.widgets import DataTable, Static

# ---------------------------------------------------------------- fixtures


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for line in lines:
            # compact separators, matching what claude/codex actually write
            fh.write(json.dumps(line, separators=(",", ":")) + "\n")


@pytest.fixture
def claude_root(tmp_path: Path) -> Path:
    root = tmp_path / "claude" / "projects"
    _write_jsonl(
        root / "-tmp-proj" / "sess-1.jsonl",
        [
            {"type": "ai-title", "aiTitle": "Test session", "sessionId": "sess-1"},
            {"type": "user", "cwd": "/tmp/proj", "message": {"role": "user", "content": "hello world"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}],
                                              "usage": {"input_tokens": 10, "output_tokens": 5,
                                                        "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1}}},
            {"type": "user", "cwd": "/tmp/proj", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "user", "cwd": "/tmp/proj", "message": {"role": "user", "content": [{"type": "text", "text": "second prompt"}]}},
        ],
    )
    return root


@pytest.fixture
def codex_root(tmp_path: Path) -> Path:
    root = tmp_path / "codex"
    sid = "1111-2222-3333-4444-555555555555"
    _write_jsonl(
        root / "sessions" / "2026" / "07" / "19" / f"rollout-2026-07-19T10-00-00-{sid}.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": sid, "id": sid, "cwd": "/tmp/cx", "thread_source": "user"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the bug"}]}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 100}}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 250}}}},
        ],
    )
    # a subagent rollout for the same thread: must be excluded
    _write_jsonl(
        root / "sessions" / "2026" / "07" / "19" / "rollout-2026-07-19T11-00-00-9999-0000-0000-0000-000000000000.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": sid, "id": "9999-0000-0000-0000-000000000000", "cwd": "/tmp/cx", "thread_source": "subagent"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "subagent prompt"}]}},
        ],
    )
    with (root / "session_index.jsonl").open("w") as fh:
        fh.write(json.dumps({"id": sid, "thread_name": "Bug fix thread", "updated_at": "2026-07-19T10:00:00Z"}) + "\n")
    return root


@pytest.fixture
def opencode_db(tmp_path: Path) -> Path:
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE session (id text PRIMARY KEY, title text, directory text,
              tokens_input integer, tokens_output integer, tokens_reasoning integer,
              cost real, time_created integer, time_updated integer, time_archived integer)"""
    )
    con.execute(
        "INSERT INTO session VALUES ('ses_1', 'OC session', '/tmp/oc', 100, 40, 10, 0.5, 1784500000000, 1784500060000, NULL)"
    )
    con.execute(
        "INSERT INTO session VALUES ('ses_2', 'Archived', '/tmp/oc', 1, 1, 1, 0.0, 1784500000000, 1784500060000, 1784500070000)"
    )
    con.commit()
    con.close()
    return db


# ---------------------------------------------------------------- collectors


def test_claude_collector(claude_root: Path):
    sessions = collectors.collect_claude(root=claude_root)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.tool == "claude"
    assert s.id == "sess-1"
    assert s.title == "Test session"
    assert s.project_dir == "/tmp/proj"
    assert s.tokens == 18
    assert s.n_messages == 3  # 2 real user prompts + 1 assistant; tool_result excluded
    assert s.first_prompt == "hello world"


def test_codex_collector_merges_and_filters(codex_root: Path):
    sessions = collectors.collect_codex(root=codex_root)
    assert len(sessions) == 1  # subagent rollout excluded
    s = sessions[0]
    assert s.tool == "codex"
    assert s.id == "1111-2222-3333-4444-555555555555"
    assert s.title == "Bug fix thread"
    assert s.tokens == 250  # cumulative: last token_count event wins
    assert s.first_prompt == "fix the bug"


def test_opencode_collector(opencode_db: Path):
    sessions = collectors.collect_opencode(db_path=opencode_db)
    assert len(sessions) == 1  # archived session excluded
    s = sessions[0]
    assert s.id == "ses_1"
    assert s.tokens == 150
    assert s.cost == 0.5
    assert s.project_dir == "/tmp/oc"


def test_cache_roundtrip(claude_root: Path, tmp_path: Path):
    first = collectors.collect_all.__wrapped__ if hasattr(collectors.collect_all, "__wrapped__") else None
    cache: dict = {}
    s1 = collectors.collect_claude(root=claude_root, cache=cache)
    assert len(cache) == 1
    s2 = collectors.collect_claude(root=claude_root, cache=cache)
    assert [s.id for s in s1] == [s.id for s in s2]
    assert s1[0].tokens == s2[0].tokens


# ---------------------------------------------------------------- resume


def test_resume_command_quotes_paths(tmp_path: Path):
    spaced = tmp_path / "dir with spaces"
    spaced.mkdir()
    s = Session(tool="claude", id="abc", title="t", project_dir=str(spaced),
                last_active=datetime.now(timezone.utc))
    assert resume_command(s) == f"cd '{spaced}' && claude --resume abc"


def test_resume_command_missing_dir_falls_home():
    s = Session(tool="codex", id="xyz", title="t", project_dir="/does/not/exist",
                last_active=datetime.now(timezone.utc))
    cmd = resume_command(s)
    assert "codex resume xyz" in cmd
    assert "/does/not/exist" not in cmd


# ---------------------------------------------------------------- usage


def test_parse_claude_usage_payload():
    from usage import _parse_claude_usage

    payload = {
        "five_hour": {"utilization": 42.0, "resets_at": "2026-07-19T20:00:00+00:00"},
        "seven_day": {"utilization": 15.0, "resets_at": "2026-07-21T01:00:00+00:00"},
    }
    windows = _parse_claude_usage(payload)
    assert [w.label for w in windows] == ["5h", "7d"]
    assert windows[0].pct == 42.0
    assert windows[0].resets_at is not None


def test_codex_usage_from_rollouts(codex_root: Path):
    from usage import fetch_codex_usage

    # add a rate_limits event to the user-thread rollout
    rollout = next((codex_root / "sessions").glob("**/*.jsonl"))
    with rollout.open("a") as fh:
        fh.write(json.dumps(
            {"type": "event_msg", "payload": {"type": "token_count", "info": {},
             "rate_limits": {"plan_type": "pro",
                             "primary": {"used_percent": 83.0, "window_minutes": 10080, "resets_at": 1784949909},
                             "secondary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1784900000}}}},
            separators=(",", ":")) + "\n")
    usage = fetch_codex_usage(root=codex_root)
    assert usage.error is None
    assert usage.plan == "Pro"
    labels = [w.label for w in usage.windows]
    assert labels == ["5h", "7d"]
    assert usage.windows[1].pct == 83.0


def test_opencode_usage_spend(opencode_db: Path):
    from usage import fetch_opencode_usage

    usage = fetch_opencode_usage(db_path=opencode_db)
    assert usage.error is None
    assert "$0.50" in usage.note
    assert "1 sessions" in usage.note


# ---------------------------------------------------------------- titles


def test_claude_title_from_slash_command(tmp_path: Path):
    root = tmp_path / "claude" / "projects"
    _write_jsonl(
        root / "-tmp-proj" / "sess-cmd.jsonl",
        [
            {"type": "user", "cwd": "/tmp/proj", "message": {"role": "user",
             "content": "<command-message>fix-pr</command-message>\n<command-name>/fix-pr-feedback</command-name>\n<command-args>3059</command-args>"}},
            {"type": "user", "cwd": "/tmp/proj", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
        ],
    )
    sessions = collectors.collect_claude(root=root)
    assert sessions[0].title == "/fix-pr-feedback 3059"


def test_claude_title_skips_injected_messages(tmp_path: Path):
    root = tmp_path / "claude" / "projects"
    _write_jsonl(
        root / "-tmp-proj" / "sess-skill.jsonl",
        [
            {"type": "user", "cwd": "/tmp/proj", "message": {"role": "user", "content": "Base directory for this skill: /Users/x/skills/review"}},
            {"type": "user", "cwd": "/tmp/proj", "message": {"role": "user", "content": "actually review my PR please"}},
        ],
    )
    sessions = collectors.collect_claude(root=root)
    assert sessions[0].title == "Actually review my PR please"


# ---------------------------------------------------------------- tui


def test_app_new_session_opens_warp_tab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch)
    monkeypatch.setattr(app_module, "_warp_configs_dir", lambda: tmp_path / "tab_configs")
    opened = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            opened.append(args)

    monkeypatch.setattr(app_module, "Popen", FakePopen)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 32)) as pilot:
            await _wait_for_table(pilot, app)
            await pilot.press("x")  # codex launches immediately in the cwd
            await pilot.pause(0.1)

    asyncio.run(run())
    slug = app_module._dir_slug(os.getcwd())
    assert opened == [["open", f"warp://tab_config/josephcode-codex-{slug}"]]
    config = (tmp_path / "tab_configs" / f"josephcode-codex-{slug}.toml").read_text()
    assert 'commands = ["codex"]' in config
    assert f'directory = "{os.getcwd()}"' in config


def test_banner_art_picks_a_font_that_fits():
    import pyfiglet

    slant = pyfiglet.figlet_format("JosephCode", font="slant").rstrip()
    small = pyfiglet.figlet_format("JosephCode", font="small").rstrip()
    slant_w = max(len(line) for line in slant.splitlines())
    small_w = max(len(line) for line in small.splitlines())

    assert app_module._banner_art(slant_w) == slant
    assert app_module._banner_art(slant_w - 1) == small
    assert app_module._banner_art(small_w) == small
    assert app_module._banner_art(small_w - 1) == ""  # nothing fits: plain-text fallback


def test_banner_never_exceeds_its_width(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # a banner wider than the Static gets word-wrapped mid-glyph, shredding the
    # art into diagonal fragments; at every terminal width it must fit instead
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch)

    async def check(width: int) -> None:
        app = AdashApp(cmd_file=str(tmp_path / f"cmd-{width}"))
        async with app.run_test(size=(width, 32)) as pilot:
            await _wait_for_table(pilot, app)
            banner = app.query_one("#banner", Static)
            plain = banner.content.replace("[bold]", "").replace("[/]", "")
            for line in plain.splitlines():
                assert len(line) <= banner.content_region.width

    async def run() -> None:
        for width in (130, 66, 62, 61, 52, 50, 49, 40, 24):
            await check(width)

    asyncio.run(run())


# ---------------------------------------------------------------- agents


def test_parse_ps_detects_only_terminal_agents():
    from agents import _parse_ps

    ps_output = """\
 64028 ttys003   01:48:12 opencode
 59001 ttys005      12:03 /Users/josephtutera/.local/bin/claude --resume abc-123
 59002 ttys005      12:03 /bin/zsh -c claude --resume abc-123
  1046 ??      07-09:23:04 /Applications/Claude.app/Contents/Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler --monitor-self
 30591 ??      07-03:02:18 node /Users/x/Repos/prototype-ehr/.claude/worktrees/foo/platform/node_modules/.bin/../tsx/dist/cli.mjs watch src/index.ts
 60200 ttys007      03:45 node /Users/josephtutera/.nvm/versions/node/v24.14.1/bin/codex
 61000 ttys008      01:10 nvim claude.md
"""
    agents = _parse_ps(ps_output)
    tools = sorted(a.tool for a in agents)
    assert tools == ["claude", "codex", "opencode"]  # shell wrapper, desktop app, dev server, editor all excluded
    opencode = next(a for a in agents if a.tool == "opencode")
    assert opencode.pid == 64028
    assert opencode.tty == "ttys003"
    assert opencode.elapsed == "1h 48m"


def test_elapsed_formatting():
    from agents import _elapsed

    assert _elapsed("12:03") == "12m"
    assert _elapsed("01:48:12") == "1h 48m"
    assert _elapsed("07-09:23:04") == "7d 9h"


def test_title_for_matches_session_by_cwd():
    from agents import RunningAgent

    agent = RunningAgent(tool="claude", pid=1, tty="ttys003", elapsed="5m", cwd="/tmp/proj")
    app = AdashApp()
    app.sessions = [
        Session(tool="claude", id="a", title="Older", project_dir="/tmp/proj",
                last_active=datetime(2026, 7, 18, tzinfo=timezone.utc)),
        Session(tool="claude", id="b", title="Newest", project_dir="/tmp/proj",
                last_active=datetime(2026, 7, 19, tzinfo=timezone.utc)),
        Session(tool="codex", id="c", title="Wrong tool", project_dir="/tmp/proj",
                last_active=datetime(2026, 7, 19, tzinfo=timezone.utc)),
    ]
    app.sessions.sort(key=lambda s: s.last_active, reverse=True)
    assert app._title_for(agent) == "Newest"


# ---------------------------------------------------------------- tui


def _fake_sessions() -> list[Session]:
    now = datetime.now(timezone.utc)
    return [
        Session(tool="claude", id="abc", title="Claude one", project_dir="/tmp",
                last_active=now, tokens=10, n_messages=2, first_prompt="hello"),
        Session(tool="codex", id="xyz", title="Codex one", project_dir="/tmp",
                last_active=now, tokens=20, n_messages=4, first_prompt="fix it"),
    ]


def _stub_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    from usage import ToolUsage, UsageWindow

    usages = [
        ToolUsage(tool="claude", plan="Team", windows=[UsageWindow("5h", 10.0), UsageWindow("7d", 15.0)]),
        ToolUsage(tool="codex", plan="Pro", windows=[UsageWindow("7d", 83.0)]),
        ToolUsage(tool="opencode", note="no subscription · API spend 7d: $1.00 across 2 sessions"),
    ]
    monkeypatch.setattr(app_module, "collect_usage",
                        lambda active_claude=None: (usages, datetime.now(timezone.utc)))


def _stub_running(monkeypatch: pytest.MonkeyPatch, agents: list | None = None) -> None:
    monkeypatch.setattr(app_module, "running_agents", lambda: agents or [])


async def _wait_for_table(pilot, app) -> None:
    for _ in range(100):
        await pilot.pause(0.05)
        if app.query_one("#sessions", DataTable).row_count:
            break


def test_app_filter_and_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch)
    cmd_file = tmp_path / "cmd"

    async def run() -> None:
        app = AdashApp(cmd_file=str(cmd_file))
        async with app.run_test(size=(110, 32)) as pilot:
            table = app.query_one("#sessions", DataTable)
            await _wait_for_table(pilot, app)
            assert table.row_count == 2

            await pilot.press("3")  # filter to codex only
            await pilot.pause(0.1)
            assert table.row_count == 1

            await pilot.press("1")  # back to all
            await pilot.pause(0.1)
            assert table.row_count == 2

            # chain is launch -> picker(dirs) -> history (no running agents)
            assert len(app.dirs) == 2  # cwd + /tmp from the fake sessions
            await pilot.press("down")  # launch -> picker[0]
            await pilot.pause(0.05)
            assert app.zone == "picker"
            await pilot.press("down")  # picker[0] -> picker[1]
            await pilot.press("down")  # picker[1] (last) -> history
            await pilot.pause(0.1)
            assert app.zone == "history"

            await pilot.press("enter")  # resume first row (claude)
            await pilot.pause(0.1)

    asyncio.run(run())
    assert cmd_file.read_text().strip() == "cd /tmp && claude --resume abc"


def test_selection_starts_on_claude_and_navigates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 32)) as pilot:
            await _wait_for_table(pilot, app)
            assert app.zone == "launch"
            assert app.launch_idx == 0  # starts on claude

            await pilot.press("right")
            await pilot.pause(0.1)
            assert app.launch_idx == 1  # codex

            await pilot.press("right")
            await pilot.pause(0.1)
            assert app.launch_idx == 2  # opencode

            await pilot.press("left")
            await pilot.pause(0.1)
            assert app.launch_idx == 1

            await pilot.press("down")  # launch -> picker (directory list)
            await pilot.pause(0.1)
            assert app.zone == "picker"

            await pilot.press("up")  # picker[0] -> back to launcher chips
            await pilot.pause(0.1)
            assert app.zone == "launch"

    asyncio.run(run())


def test_navigation_through_active_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agents import RunningAgent

    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch, agents=[
        RunningAgent(tool="claude", pid=1, tty="ttys001", elapsed="5m", cwd="/tmp"),
        RunningAgent(tool="codex", pid=2, tty="ttys002", elapsed="9m", cwd="/tmp"),
    ])

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 32)) as pilot:
            await _wait_for_table(pilot, app)
            for _ in range(100):
                await pilot.pause(0.05)
                if len(app.running) == 2:
                    break

            # chain: launch -> picker(2 dirs) -> active(2) -> history
            assert len(app.dirs) == 2
            await pilot.press("down")  # launch -> picker[0]
            await pilot.press("down")  # picker[0] -> picker[1]
            await pilot.pause(0.05)
            assert app.zone == "picker"
            await pilot.press("down")  # picker[1] (last) -> active[0]
            await pilot.pause(0.1)
            assert app.zone == "active" and app.active_idx == 0

            await pilot.press("down")  # -> active[1]
            await pilot.pause(0.1)
            assert app.active_idx == 1

            await pilot.press("down")  # last active -> history
            await pilot.pause(0.1)
            assert app.zone == "history"

            await pilot.press("up")  # history row 0 -> active[last]
            await pilot.pause(0.1)
            assert app.zone == "active" and app.active_idx == 1

            await pilot.press("up")  # active[1] -> active[0]
            await pilot.press("up")  # active[0] -> picker[last]
            await pilot.pause(0.1)
            assert app.zone == "picker"

    asyncio.run(run())


def test_clear_and_undo_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from datetime import timedelta

    old = datetime.now(timezone.utc) - timedelta(days=2)
    sessions = _fake_sessions() + [
        Session(tool="claude", id="old", title="Old session", project_dir="/tmp", last_active=old)
    ]
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: sessions)
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch)
    monkeypatch.setattr(app_module, "_cleared_file", lambda: tmp_path / "cleared-at")

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 32)) as pilot:
            table = app.query_one("#sessions", DataTable)
            await _wait_for_table(pilot, app)
            assert table.row_count == 3

            await pilot.press("C")  # clear: hide everything seen so far
            await pilot.pause(0.1)
            assert table.row_count == 0

            await pilot.press("u")  # undo
            await pilot.pause(0.1)
            assert table.row_count == 3

    asyncio.run(run())


# ---------------------------------------------------------------- titles


def test_clean_title():
    from models import clean_title

    assert clean_title("can you help me update my personal claude.md") == "Update my personal claude.md"
    assert clean_title("please check out the dev branch") == "Check out the dev branch"
    assert clean_title("If i wanted to use datadog") == "If i wanted to use datadog"
    assert clean_title("https://finance.yahoo.com/quote/CCLD/ if i wanted") == "https://finance.yahoo.com/quote/CCLD/ if i wanted"
    long_prompt = "word " * 30
    result = clean_title(long_prompt.strip(), width=40)
    assert len(result) <= 41 and result.endswith("…") and "  " not in result


def test_clean_title_strips_extended_filler():
    from models import clean_title

    # dictated-style filler and typos should peel off, leaving a readable title
    assert clean_title("can u set up the onepass cli") == "Set up the onepass cli"
    assert clean_title("pls pull down dev for web app") == "Pull down dev for web app"
    assert clean_title("just fix the laborder empty unit") == "Fix the laborder empty unit"
    assert clean_title("so i wanna audit the CarePilot screens") == "Audit the CarePilot screens"
    # stacked filler still peels within the pass budget
    assert clean_title("ok so can you help me brainstorm") == "Brainstorm"


# ---------------------------------------------------------------- terminal launcher


def test_launch_tools_includes_terminal():
    assert app_module.LAUNCH_TOOLS == ("claude", "codex", "opencode", "terminal")


def test_terminal_tab_config_opens_bare_shell(tmp_path: Path):
    stem = app_module._write_tab_config("terminal", "/tmp/proj", tmp_path)
    assert stem == "josephcode-terminal"
    config = (tmp_path / "josephcode-terminal.toml").read_text()
    assert 'directory = "/tmp/proj"' in config
    assert "commands" not in config  # a plain shell, no agent command
    assert 'name = "terminal · proj"' in config


def test_agent_tab_config_still_has_command(tmp_path: Path):
    app_module._write_tab_config("codex", "/tmp/proj", tmp_path)
    config = (tmp_path / "josephcode-codex.toml").read_text()
    assert 'commands = ["codex"]' in config


def test_bar_color_ramp():
    # calm blue below 60, amber in the warning band, red when critical
    assert "#7aa2f7" in app_module._bar(41.0)
    assert "#ffb454" in app_module._bar(62.0)
    assert "#ff5c57" in app_module._bar(100.0)


# ---------------------------------------------------------------- live activity


def test_claude_activity_status_and_frontmost_label(tmp_path: Path):
    from activity import claude_activity

    root = tmp_path / "claude"
    (root / "sessions").mkdir(parents=True)
    (root / "statusbar").mkdir(parents=True)
    (root / "sessions" / "4523.json").write_text(json.dumps({"sessionId": "S1", "status": "busy"}))
    (root / "sessions" / "999.json").write_text(json.dumps({"sessionId": "S2", "status": "idle"}))
    (root / "statusbar" / "state.json").write_text(
        json.dumps({"sessionId": "S1", "state": "tool", "label": "Running command", "tool": "Bash"})
    )

    acts = claude_activity(root)
    assert acts[4523].state == "working"
    assert acts[4523].label == "Running command"  # frontmost session gets the live action
    assert acts[999].state == "idle"
    assert acts[999].label == ""  # not frontmost -> no action detail


def test_claude_activity_waiting_on_permission(tmp_path: Path):
    from activity import claude_activity

    root = tmp_path / "claude"
    (root / "sessions").mkdir(parents=True)
    (root / "statusbar").mkdir(parents=True)
    (root / "sessions" / "12.json").write_text(json.dumps({"sessionId": "S1", "status": "busy"}))
    (root / "statusbar" / "state.json").write_text(
        json.dumps({"sessionId": "S1", "state": "permission", "label": "Awaiting permission"})
    )
    assert claude_activity(root)[12].state == "waiting"


def test_codex_activity_working_then_idle(tmp_path: Path):
    from activity import codex_activity

    day = tmp_path / "codex" / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    _write_jsonl(day / "rollout-a.jsonl", [
        {"timestamp": "t", "type": "session_meta", "payload": {"session_id": "c1"}},
        {"timestamp": "t", "type": "event_msg",
         "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 1234}}}},
        {"timestamp": "t", "type": "response_item", "payload": {"type": "function_call"}},
    ])
    st = codex_activity(tmp_path / "codex" / "sessions")
    assert st.state == "working" and st.tokens == 1234 and "tool" in st.label

    # a newer rollout that has finished its turn
    _write_jsonl(day / "rollout-b.jsonl", [
        {"timestamp": "t", "type": "event_msg",
         "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 40}}}},
        {"timestamp": "t", "type": "event_msg", "payload": {"type": "task_complete"}},
    ])
    st2 = codex_activity(tmp_path / "codex" / "sessions")
    assert st2.state == "idle" and st2.tokens == 40


def test_codex_activity_none_when_empty(tmp_path: Path):
    from activity import codex_activity
    assert codex_activity(tmp_path / "nope") is None


def test_opencode_activity_generating(tmp_path: Path):
    from activity import opencode_activity

    db = tmp_path / "oc.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE session (id TEXT, tokens_input INT, tokens_output INT, "
                "tokens_reasoning INT, time_updated INT, time_archived INT)")
    con.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, data TEXT)")
    con.execute("INSERT INTO session VALUES ('s1', 100, 50, 10, 2000, NULL)")
    con.execute("INSERT INTO message VALUES ('m1', 's1', 1000, ?)",
                (json.dumps({"role": "assistant", "time": {"created": 1, "completed": None}}),))
    con.commit()
    con.close()

    st = opencode_activity(db)
    assert st.tokens == 160 and st.state == "working"


def test_enrich_matches_claude_by_pid(tmp_path: Path):
    from activity import enrich
    from agents import RunningAgent

    root = tmp_path / "claude"
    (root / "sessions").mkdir(parents=True)
    (root / "statusbar").mkdir(parents=True)
    (root / "sessions" / "1.json").write_text(json.dumps({"sessionId": "S1", "status": "busy"}))
    (root / "statusbar" / "state.json").write_text(
        json.dumps({"sessionId": "S1", "state": "tool", "label": "Running command"}))

    agents = [
        RunningAgent(tool="claude", pid=1, tty="t1", elapsed="5m", cwd="/tmp"),
        RunningAgent(tool="codex", pid=2, tty="t2", elapsed="9m", cwd="/tmp"),
    ]
    enrich(agents, claude_root=root, codex_root=tmp_path / "no-codex",
           opencode_db=tmp_path / "no.db")
    assert agents[0].state == "working" and agents[0].label == "Running command"
    assert agents[1].state == "unknown"  # no codex rollout -> left untouched


# ---------------------------------------------------------------- directory picker


def test_dir_slug():
    assert app_module._dir_slug("/Users/joseph/Repos/agent-dash") == "agent-dash"
    assert app_module._dir_slug("/Users/joseph/Repos/web_app.v2") == "web-app-v2"
    assert app_module._dir_slug("/") == "root"


def _launch_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch)
    monkeypatch.setattr(app_module, "_warp_configs_dir", lambda: tmp_path / "tab_configs")
    opened: list = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            opened.append(args)

    monkeypatch.setattr(app_module, "Popen", FakePopen)
    return opened


def test_quick_key_launches_claude_in_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    opened = _launch_harness(tmp_path, monkeypatch)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 40)) as pilot:
            await _wait_for_table(pilot, app)
            await pilot.press("c")  # quick-launch claude in the current dir
            await pilot.pause(0.1)

    asyncio.run(run())
    slug = app_module._dir_slug(os.getcwd())
    assert opened == [["open", f"warp://tab_config/josephcode-claude-{slug}"]]
    config = (tmp_path / "tab_configs" / f"josephcode-claude-{slug}.toml").read_text()
    assert 'commands = ["claude"]' in config
    assert f'directory = "{os.getcwd()}"' in config


def test_inline_picker_multiselect_opens_multiple_tabs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    opened = _launch_harness(tmp_path, monkeypatch)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 40)) as pilot:
            await _wait_for_table(pilot, app)
            assert len(app.dirs) == 2  # cwd (preselected) + /tmp
            await pilot.press("down")   # launch -> picker[0] (cwd)
            await pilot.press("down")   # -> picker[1] (/tmp)
            await pilot.press("space")  # also select /tmp
            await pilot.pause(0.05)
            assert app.picker_selected == {0, 1}
            await pilot.press("enter")  # open both
            await pilot.pause(0.1)

    asyncio.run(run())
    assert len(opened) == 2  # cwd + /tmp
    stems = sorted(o[1].split("/")[-1] for o in opened)
    assert stems == sorted([f"josephcode-claude-{app_module._dir_slug(os.getcwd())}", "josephcode-claude-tmp"])


def test_picker_launches_selected_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    opened = _launch_harness(tmp_path, monkeypatch)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 40)) as pilot:
            await _wait_for_table(pilot, app)
            await pilot.press("right")  # chips: claude -> codex
            await pilot.pause(0.05)
            await pilot.press("down")   # into the picker (cwd preselected)
            await pilot.press("enter")  # launch the selected tool (codex) in cwd
            await pilot.pause(0.1)

    asyncio.run(run())
    slug = app_module._dir_slug(os.getcwd())
    assert opened == [["open", f"warp://tab_config/josephcode-codex-{slug}"]]


def test_recent_dirs_pins_cwd_first(monkeypatch: pytest.MonkeyPatch):
    app = AdashApp()
    app.sessions = _fake_sessions()  # both in /tmp
    dirs = app._recent_dirs()
    assert dirs[0][0] == os.getcwd()  # current directory pinned first
    assert any(d[0] == "/tmp" for d in dirs)  # history dirs follow


# ---------------------------------------------------------------- dual claude plans


def _make_claude_dir(home: Path, name: str, sub_type: str, org: str | None, token: str) -> Path:
    d = home / name
    d.mkdir(parents=True)
    account = {"emailAddress": "joseph@carepilot.com"}
    if org:
        account.update({"organizationName": org, "organizationType": "claude_team"})
    (d / ".claude.json").write_text(json.dumps({"oauthAccount": account}))
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "subscriptionType": sub_type}}))
    return d


def test_claude_profiles_discovers_extra_accounts(tmp_path: Path):
    from usage import claude_profiles

    _make_claude_dir(tmp_path, ".claude", "claude_team", "CarePilot", "tok-team")
    _make_claude_dir(tmp_path, ".claude-personal", "claude_max_20x", None, "tok-personal")

    profiles = claude_profiles(home=tmp_path)
    assert [p.label for p in profiles] == ["carepilot", "personal"]
    assert profiles[0].default is True and profiles[1].default is False
    assert profiles[1].config_dir == tmp_path / ".claude-personal"


def test_profile_credentials_from_file(tmp_path: Path):
    from usage import ClaudeProfile, _profile_credentials

    d = _make_claude_dir(tmp_path, ".claude-personal", "claude_max_20x", None, "tok-xyz")
    token, plan = _profile_credentials(ClaudeProfile(label="personal", config_dir=d))
    assert token == "tok-xyz"
    assert plan == "Max 20X"


def test_fetch_claude_usages_labels_and_active(monkeypatch: pytest.MonkeyPatch):
    import usage as usage_module
    from usage import ClaudeProfile, ToolUsage

    profiles = [
        ClaudeProfile(label="carepilot", config_dir=Path("/x/.claude"), default=True),
        ClaudeProfile(label="personal", config_dir=Path("/x/.claude-personal")),
    ]
    monkeypatch.setattr(usage_module, "claude_profiles", lambda home=None: profiles)
    monkeypatch.setattr(usage_module, "fetch_claude_usage_for",
                        lambda p: ToolUsage(tool="claude", plan=p.label.title()))

    usages = usage_module.fetch_claude_usages(active_label="personal")
    assert [u.label for u in usages] == ["carepilot", "personal"]
    assert [u.active for u in usages] == [False, True]  # personal is active


def test_write_tab_config_injects_config_dir_env(tmp_path: Path):
    stem = app_module._write_tab_config("claude", "/tmp/p", tmp_path, suffix="p",
                                        env={"CLAUDE_CONFIG_DIR": "/home/.claude-personal"})
    config = (tmp_path / f"{stem}.toml").read_text()
    assert 'commands = ["CLAUDE_CONFIG_DIR=/home/.claude-personal claude"]' in config


def test_active_claude_profile_selection(monkeypatch: pytest.MonkeyPatch):
    from usage import ClaudeProfile

    profiles = [
        ClaudeProfile(label="carepilot", config_dir=Path("/x/.claude"), default=True),
        ClaudeProfile(label="personal", config_dir=Path("/x/.claude-personal")),
    ]
    monkeypatch.setattr(app_module, "claude_profiles", lambda: profiles)
    app = AdashApp()
    app.active_claude = "personal"
    assert app._active_claude_profile().label == "personal"
    app.active_claude = None
    assert app._active_claude_profile().default is True  # falls back to default


def test_app_tab_switches_claude_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from usage import ToolUsage, UsageWindow

    two_claude = [
        ToolUsage(tool="claude", plan="Team", label="carepilot", active=True,
                  windows=[UsageWindow("5h", 12.0)]),
        ToolUsage(tool="claude", plan="Max 20x", label="personal", active=False,
                  windows=[UsageWindow("5h", 62.0)]),
        ToolUsage(tool="codex", plan="Pro", windows=[UsageWindow("7d", 40.0)]),
    ]
    monkeypatch.setattr(app_module, "collect_usage", lambda active_claude=None: (two_claude, datetime.now(timezone.utc)))
    _stub_running(monkeypatch)
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(120, 34)) as pilot:
            await _wait_for_table(pilot, app)
            for _ in range(40):
                await pilot.pause(0.05)
                if app.usages:
                    break
            assert app.active_claude == "carepilot"  # adopts the fetched active plan
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert app.active_claude == "personal"
            actives = {u.label: u.active for u in app.usages if u.tool == "claude"}
            assert actives == {"carepilot": False, "personal": True}

    asyncio.run(run())


def test_keychain_service_hashes_nondefault_config_dir():
    import hashlib
    from usage import ClaudeProfile, _keychain_service

    default = ClaudeProfile(label="team", config_dir=Path("/Users/j/.claude"), default=True)
    assert _keychain_service(default) == "Claude Code-credentials"

    personal = ClaudeProfile(label="personal", config_dir=Path("/Users/j/.claude-personal"))
    digest = hashlib.sha256(b"/Users/j/.claude-personal").hexdigest()[:8]
    assert _keychain_service(personal) == f"Claude Code-credentials-{digest}"


def test_profile_credentials_reads_hashed_keychain_when_no_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import usage as usage_module
    from usage import ClaudeProfile, _profile_credentials

    d = tmp_path / ".claude-personal"
    d.mkdir()  # no .credentials.json -> must fall back to keychain
    seen = {}

    def fake_keychain(service="Claude Code-credentials"):
        seen["service"] = service
        return "tok-personal", "Max 20X"

    monkeypatch.setattr(usage_module, "_keychain_claude_credentials", fake_keychain)
    token, plan = _profile_credentials(ClaudeProfile(label="personal", config_dir=d))
    assert token == "tok-personal" and plan == "Max 20X"
    assert seen["service"] == usage_module._keychain_service(ClaudeProfile(label="personal", config_dir=d))


# ---------------------------------------------------------------- tab title


def test_set_tab_title_emits_osc_sequence(capsys: pytest.CaptureFixture):
    import main as main_module

    main_module.set_tab_title(main_module.TAB_TITLE)
    out = capsys.readouterr().out
    # OSC 0/1/2 so Warp (and iTerm2/Terminal.app) all pick up the tab title
    for osc in (0, 1, 2):
        assert f"\x1b]{osc};{main_module.TAB_TITLE}\x07" in out


def test_set_tab_title_empty_clears_override(capsys: pytest.CaptureFixture):
    import main as main_module

    main_module.set_tab_title("")
    out = capsys.readouterr().out
    assert f"\x1b]0;\x07" in out  # empty title hands the tab back to process/cwd display


def test_set_tab_title_skips_dumb_terminals(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    import main as main_module

    monkeypatch.setenv("TERM", "dumb")
    main_module.set_tab_title(main_module.TAB_TITLE)
    assert capsys.readouterr().out == ""
