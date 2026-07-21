"""Tests for adash collectors, resume commands, and the TUI flow."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
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


def test_parse_claude_usage_includes_fable_scoped_limit():
    from usage import _parse_claude_usage

    payload = {
        "five_hour": {"utilization": 39.0, "resets_at": "2026-07-20T22:00:00+00:00"},
        "seven_day": {"utilization": 26.0, "resets_at": "2026-07-21T00:00:00+00:00"},
        "limits": [
            {"kind": "session", "percent": 39, "resets_at": "2026-07-20T22:00:00+00:00", "scope": None},
            {"kind": "weekly_all", "percent": 26, "resets_at": "2026-07-21T00:00:00+00:00", "scope": None},
            {"kind": "weekly_scoped", "percent": 32, "resets_at": "2026-07-21T00:00:00+00:00",
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        ],
    }
    windows = _parse_claude_usage(payload)
    assert [w.label for w in windows] == ["5h", "7d", "fable"]  # scoped model becomes its own window
    assert windows[-1].pct == 32.0
    assert windows[-1].resets_at is not None


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
    assert usage.plan == "pay-as-you-go"  # framed as a first-party plan, not "no subscription"
    assert usage.spend == 0.50
    assert usage.spend_sessions == 1
    assert usage.spend_days == 7


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


def test_bare_slash_command_loses_to_a_real_prompt(tmp_path: Path):
    """"/model" says nothing about which session this is, so a prompt typed
    afterwards is the better title. A command with args already describes the
    work, so that still wins."""
    root = tmp_path / "claude" / "projects"
    _write_jsonl(
        root / "-tmp-bare" / "sess-bare.jsonl",
        [
            {"type": "user", "cwd": "/tmp/bare", "message": {"role": "user",
             "content": "<command-message>model</command-message>\n<command-name>/model</command-name>"}},
            {"type": "user", "cwd": "/tmp/bare", "message": {"role": "user", "content": "wire up the billing webhook"}},
        ],
    )
    _write_jsonl(
        root / "-tmp-args" / "sess-args.jsonl",
        [
            {"type": "user", "cwd": "/tmp/args", "message": {"role": "user",
             "content": "<command-message>review</command-message>\n<command-name>/review</command-name>\n<command-args>412</command-args>"}},
            {"type": "user", "cwd": "/tmp/args", "message": {"role": "user", "content": "wire up the billing webhook"}},
        ],
    )
    titles = {s.id: s.title for s in collectors.collect_claude(root=root)}
    assert titles["sess-bare"] == "Wire up the billing webhook"
    assert titles["sess-args"] == "/review 412"


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
    assert opened == [["open", f"warp://tab_config/agentdash-codex-{slug}"]]
    config = (tmp_path / "tab_configs" / f"agentdash-codex-{slug}.toml").read_text()
    assert 'commands = ["codex"]' in config
    assert f'directory = "{os.getcwd()}"' in config


def test_banner_art_picks_a_font_that_fits():
    import pyfiglet

    trim = app_module._trim_descenders
    arts = [trim(pyfiglet.figlet_format(app_module.BANNER_TEXT, font=f).rstrip())
            for f in app_module.BANNER_FONTS]
    widths = [max(len(line) for line in art.splitlines()) for art in arts]

    assert widths == sorted(widths, reverse=True), "BANNER_FONTS must be widest-first"
    assert app_module._banner_art(widths[0]) == arts[0]
    assert app_module._banner_art(widths[0] - 1) == arts[1]
    assert app_module._banner_art(widths[-1]) == arts[-1]
    assert app_module._banner_art(widths[-1] - 1) == ""  # nothing fits: plain-text fallback


def test_banner_trims_the_orphan_g_descender():
    import pyfiglet

    # figlet slant hangs the lowercase-g tail on its own sparse line below the
    # word; the banner must sit on its dense baseline instead of that orphan
    raw = pyfiglet.figlet_format(app_module.BANNER_TEXT, font="slant").rstrip()
    raw_lines = raw.split("\n")

    def ink(line: str) -> int:
        return len(line.replace(" ", ""))

    assert ink(raw_lines[-1]) * 3 < max(ink(l) for l in raw_lines), "fixture: last line is a sparse tail"
    trimmed = app_module._trim_descenders(raw).split("\n")
    assert len(trimmed) == len(raw_lines) - 1  # the orphan descender line is gone
    assert ink(trimmed[-1]) > ink(raw_lines[-1])  # new bottom is the dense baseline
    # a block with no descender tail is left exactly as-is
    solid = "\n".join(["#####", "#   #", "#####"])
    assert app_module._trim_descenders(solid) == solid


def test_boot_sweep_pegs_then_settles_to_true():
    sweep = app_module._sweep_pct
    peak = app_module._BOOT_PEAK

    # starts empty, ends at the true reading no matter what that reading is
    assert sweep(16.0, 0.0) == 0.0
    assert sweep(16.0, 1.0) == 16.0
    assert sweep(93.0, 1.0) == 93.0
    # at the peak of the sweep every gauge is pegged to full scale
    assert sweep(16.0, peak) == pytest.approx(100.0)
    assert sweep(93.0, peak) == pytest.approx(100.0)
    # a window a tool doesn't report stays absent through the whole sweep
    assert sweep(None, 0.3) is None
    assert sweep(None, 1.0) is None
    # monotonic rise up to the peg, monotonic settle down to the true value after
    rise = [sweep(16.0, p / 100) for p in range(0, int(peak * 100) + 1)]
    assert rise == sorted(rise)
    settle = [sweep(16.0, peak + d) for d in (0.0, 0.1, 0.2, (1.0 - peak) - 0.001)]
    assert settle == sorted(settle, reverse=True)
    # never overshoots outside the [true, peg] envelope
    for p in range(0, 101):
        assert 0.0 <= sweep(16.0, p / 100) <= 100.0


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


def _titled_app(*agents):
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
    app.running = list(agents)
    app._resolve_agent_titles()
    return app


def test_title_for_matches_session_by_cwd():
    from agents import RunningAgent

    agent = RunningAgent(tool="claude", pid=1, tty="ttys003", elapsed="5m", cwd="/tmp/proj")
    assert _titled_app(agent)._title_for(agent) == "Newest"


def test_title_for_prefers_the_exact_session_id():
    """The pid file names the transcript, so recency never overrides it."""
    from agents import RunningAgent

    agent = RunningAgent(tool="claude", pid=1, tty="ttys003", elapsed="5m",
                         cwd="/tmp/proj", session_id="a")
    assert _titled_app(agent)._title_for(agent) == "Older"


def test_two_agents_in_one_directory_get_different_titles():
    """Without this, both web-app tabs showed the same title and were unusable."""
    from agents import RunningAgent

    older = RunningAgent(tool="claude", pid=100, tty="ttys001", elapsed="2h", cwd="/tmp/proj")
    newer = RunningAgent(tool="claude", pid=900, tty="ttys002", elapsed="5m", cwd="/tmp/proj")
    app = _titled_app(older, newer)
    assert app._title_for(newer) == "Newest"  # newest process -> newest transcript
    assert app._title_for(older) == "Older"


def test_title_falls_back_to_directory_when_sessions_run_out():
    from agents import RunningAgent

    a = RunningAgent(tool="claude", pid=1, tty="ttys001", elapsed="1m", cwd="/tmp/proj")
    b = RunningAgent(tool="claude", pid=2, tty="ttys002", elapsed="1m", cwd="/tmp/proj")
    c = RunningAgent(tool="claude", pid=3, tty="ttys003", elapsed="1m", cwd="/tmp/proj")
    app = _titled_app(a, b, c)  # only two claude sessions exist for this dir
    assert app._title_for(a) == "proj"


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
                        lambda active_claude=None, force=False: (usages, datetime.now(timezone.utc)))


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

            # chain is launch -> picker(dirs + "other" row) -> history (no running agents)
            assert len(app.dirs) == 2  # cwd + /tmp from the fake sessions
            await pilot.press("down")  # launch -> picker[0]
            await pilot.pause(0.05)
            assert app.zone == "picker"
            await pilot.press("down")  # picker[0] -> picker[1]
            await pilot.press("down")  # picker[1] -> "open another directory" row
            await pilot.press("down")  # "other" row (last) -> history
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

            # chain: launch -> picker(2 dirs + "other" row) -> active(2) -> history
            assert len(app.dirs) == 2
            await pilot.press("down")  # launch -> picker[0]
            await pilot.press("down")  # picker[0] -> picker[1]
            await pilot.press("down")  # picker[1] -> "open another directory" row
            await pilot.pause(0.05)
            assert app.zone == "picker"
            await pilot.press("down")  # "other" row (last) -> active[0]
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


def test_focus_warp_tab_cycles_until_title_matches(monkeypatch: pytest.MonkeyPatch):
    titles = ["~ agent-dash", "✳ Some other tab", "✳ Design bug fix and GitHub push"]
    state = {"i": 0}
    calls = []

    def fake_osa(script: str) -> tuple[bool, str]:
        calls.append(script)
        if "keystroke" in script:
            state["i"] += 1  # each next-tab keystroke advances the active tab
            return True, ""
        if "activate" in script:
            return True, ""
        return True, titles[state["i"]]

    monkeypatch.setattr(app_module, "_osa", fake_osa)
    assert app_module._focus_warp_tab(["design bug fix"]) == "focused"
    assert sum("keystroke" in c for c in calls) == 2  # skipped two unrelated tabs
    assert any("activate" in c for c in calls)  # Warp always comes to the front


def test_focus_warp_tab_denied_without_accessibility(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def fake_osa(script: str) -> tuple[bool, str]:
        calls.append(script)
        return False, "osascript is not allowed assistive access. (-1719)"

    monkeypatch.setattr(app_module, "_osa", fake_osa)
    assert app_module._focus_warp_tab(["anything"]) == "denied"
    assert not any("keystroke" in c for c in calls)  # no point cycling blind


def _focus_recorder(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    def fake_focus(hints: list[str], max_tabs: int = 16) -> str:
        calls.append(hints)
        return "focused"

    monkeypatch.setattr(app_module, "_focus_warp_tab", fake_focus)


async def _wait_for_focus_call(pilot, calls: list) -> None:
    for _ in range(100):
        await pilot.pause(0.05)
        if calls:
            break


def test_enter_on_active_row_jumps_to_tab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agents import RunningAgent

    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch, agents=[
        # cwd matches no fake session, so the display title is the dir basename
        RunningAgent(tool="claude", pid=1, tty="ttys001", elapsed="5m",
                     cwd="/Users/test/web-app"),
    ])
    calls: list = []
    _focus_recorder(monkeypatch, calls)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 32)) as pilot:
            await _wait_for_table(pilot, app)
            for _ in range(100):
                await pilot.pause(0.05)
                if app.running:
                    break
            for _ in range(10):  # walk the chain down into the active zone
                await pilot.press("down")
                await pilot.pause(0.05)
                if app.zone == "active":
                    break
            assert app.zone == "active"
            await pilot.press("enter")
            await _wait_for_focus_call(pilot, calls)

    asyncio.run(run())
    assert calls and "web-app" in calls[0]


def test_click_active_row_jumps_to_tab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agents import RunningAgent

    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch, agents=[
        RunningAgent(tool="claude", pid=1, tty="ttys001", elapsed="5m",
                     cwd="/Users/test/web-app", state="working",
                     label="editing auth.py", tokens=12_000),
        RunningAgent(tool="codex", pid=2, tty="ttys002", elapsed="9m",
                     cwd="/Users/test/api-server"),
    ])
    calls: list = []
    _focus_recorder(monkeypatch, calls)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 32)) as pilot:
            await _wait_for_table(pilot, app)
            for _ in range(100):
                await pilot.pause(0.05)
                if app.running:
                    break
            # panel rows: border 1 + padding-top 1, so content line 0 is y=2;
            # the first agent also has a detail line at y=3
            await pilot.click("#active", offset=(5, 2))
            await _wait_for_focus_call(pilot, calls)
            assert calls and "web-app" in calls[0]
            assert app.zone == "active" and app.active_idx == 0

            calls.clear()
            await pilot.click("#active", offset=(5, 4))  # second agent's row
            await _wait_for_focus_call(pilot, calls)
            assert calls and "api-server" in calls[0]
            assert app.active_idx == 1

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
    assert stem == "agentdash-terminal"
    config = (tmp_path / "agentdash-terminal.toml").read_text()
    assert 'directory = "/tmp/proj"' in config
    assert "commands" not in config  # a plain shell, no agent command
    assert 'name = "terminal · proj"' in config


def test_agent_tab_config_still_has_command(tmp_path: Path):
    app_module._write_tab_config("codex", "/tmp/proj", tmp_path)
    config = (tmp_path / "agentdash-codex.toml").read_text()
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
    assert opened == [["open", f"warp://tab_config/agentdash-claude-{slug}"]]
    config = (tmp_path / "tab_configs" / f"agentdash-claude-{slug}.toml").read_text()
    assert 'commands = ["claude"]' in config
    assert f'directory = "{os.getcwd()}"' in config


def test_inline_picker_opens_a_tab_per_enter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Enter opens exactly the highlighted directory and leaves the picker up,
    so several tabs is several presses rather than a checkbox multi-select."""
    opened = _launch_harness(tmp_path, monkeypatch)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 40)) as pilot:
            await _wait_for_table(pilot, app)
            assert len(app.dirs) == 2  # cwd + /tmp
            await pilot.press("down")   # launch -> picker[0] (cwd)
            await pilot.press("enter")  # opens cwd only
            await pilot.pause(0.1)
            assert len(opened) == 1
            assert app.zone == "picker"  # picker stays up for the next one
            await pilot.press("down")   # -> picker[1] (/tmp)
            await pilot.press("enter")
            await pilot.pause(0.1)
            # the footer echoes both directories opened this run
            assert app.picker_opened == [os.getcwd(), "/tmp"]

    asyncio.run(run())
    assert len(opened) == 2  # cwd + /tmp
    stems = sorted(o[1].split("/")[-1] for o in opened)
    assert stems == sorted([f"agentdash-claude-{app_module._dir_slug(os.getcwd())}", "agentdash-claude-tmp"])


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
    assert opened == [["open", f"warp://tab_config/agentdash-codex-{slug}"]]


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
                        lambda p, force=False: ToolUsage(tool="claude", plan=p.label.title()))

    usages = usage_module.fetch_claude_usages(active_label="personal")
    assert [u.label for u in usages] == ["carepilot", "personal"]
    assert [u.active for u in usages] == [False, True]  # personal is active


# ------------------------------------------------- usage cache + rate limiting


@pytest.fixture
def usage_probe(monkeypatch: pytest.MonkeyPatch):
    """A stubbed usage endpoint with a call counter, plus a clean cache.

    Returns (profile, probe) where probe.calls counts endpoint hits and
    probe.fail is set to a RateLimited instance to make the next call 429.
    """
    import usage as usage_module
    from usage import ClaudeProfile, ToolUsage, UsageWindow

    usage_module._claude_cache.clear()

    class Probe:
        calls = 0
        fail: Exception | None = None  # set to RateLimited to make the next call 429
        error: str | None = None  # set to simulate a transient (non-429) failure
        pct = 40.0

    probe = Probe()

    def fake_fetch(token: str, plan: str) -> ToolUsage:
        probe.calls += 1
        if probe.fail is not None:
            raise probe.fail
        if probe.error is not None:
            return ToolUsage(tool="claude", plan=plan, error=probe.error)
        return ToolUsage(tool="claude", plan=plan, windows=[UsageWindow("5h", probe.pct)])

    monkeypatch.setattr(usage_module, "_profile_credentials", lambda p: ("tok", "Max"))
    monkeypatch.setattr(usage_module, "_claude_usage_from_token", fake_fetch)
    yield ClaudeProfile(label="personal", config_dir=Path("/x/.claude-personal")), probe
    usage_module._claude_cache.clear()


def test_claude_usage_serves_cache_within_ttl(usage_probe):
    import usage as usage_module

    profile, probe = usage_probe
    first = usage_module.fetch_claude_usage_for(profile)
    second = usage_module.fetch_claude_usage_for(profile)

    assert probe.calls == 1  # the second read never touched the endpoint
    assert first.windows[0].pct == second.windows[0].pct == 40.0
    assert not second.stale  # a fresh cache hit isn't flagged as old

    # the manual refresh key skips the freshness check
    usage_module.fetch_claude_usage_for(profile, force=True)
    assert probe.calls == 2

    # ...but an expired entry refetches on its own
    entry = usage_module._claude_cache[str(profile.config_dir)]
    entry.fetched_at -= usage_module.USAGE_CACHE_TTL_SECONDS + 1
    usage_module.fetch_claude_usage_for(profile)
    assert probe.calls == 3


def test_claude_cache_hands_out_copies(usage_probe):
    """fetch_claude_usages stamps .label/.active on what it gets back; if that
    were the cached object those stamps would leak into later reads."""
    import usage as usage_module

    profile, _ = usage_probe
    first = usage_module.fetch_claude_usage_for(profile)
    first.label = "personal"
    first.active = False
    second = usage_module.fetch_claude_usage_for(profile)
    assert second.label == "" and second.active is True


def test_claude_429_keeps_last_bars_and_backs_off(usage_probe):
    import usage as usage_module

    profile, probe = usage_probe
    usage_module.fetch_claude_usage_for(profile)  # prime the cache with real bars
    probe.fail = usage_module.RateLimited()

    limited = usage_module.fetch_claude_usage_for(profile, force=True)
    assert limited.windows[0].pct == 40.0  # last good reading survives the 429
    assert limited.error is None  # no raw "HTTP Error 429" splattered on the row
    assert "rate limited" in limited.stale

    # inside the cooldown we stop asking entirely, even when the user mashes `r`
    calls_before = probe.calls
    again = usage_module.fetch_claude_usage_for(profile, force=True)
    assert probe.calls == calls_before
    assert "rate limited" in again.stale

    entry = usage_module._claude_cache[str(profile.config_dir)]
    assert entry.backoff == usage_module.RATE_LIMIT_BACKOFF_START

    # a repeat 429 once the cooldown lapses doubles the penalty
    entry.cooldown_until = 0.0
    usage_module.fetch_claude_usage_for(profile, force=True)
    assert entry.backoff == usage_module.RATE_LIMIT_BACKOFF_START * 2
    assert entry.backoff <= usage_module.RATE_LIMIT_BACKOFF_MAX


def test_claude_429_recovery_clears_backoff(usage_probe):
    import usage as usage_module

    profile, probe = usage_probe
    usage_module.fetch_claude_usage_for(profile)
    probe.fail = usage_module.RateLimited()
    usage_module.fetch_claude_usage_for(profile, force=True)

    entry = usage_module._claude_cache[str(profile.config_dir)]
    entry.cooldown_until = 0.0
    probe.fail, probe.pct = None, 55.0
    recovered = usage_module.fetch_claude_usage_for(profile, force=True)

    assert recovered.windows[0].pct == 55.0
    assert not recovered.stale
    assert entry.backoff == 0.0 and entry.cooldown_until == 0.0


def test_claude_429_honors_retry_after(usage_probe):
    import usage as usage_module

    profile, probe = usage_probe
    probe.fail = usage_module.RateLimited(retry_after=45.0)
    limited = usage_module.fetch_claude_usage_for(profile)

    # no cached reading yet, so the marker lands in the error slot -- but short,
    # and using the server's own number rather than our default backoff
    assert limited.error == "rate limited · retry 45s"
    entry = usage_module._claude_cache[str(profile.config_dir)]
    assert 40 < entry.cooldown_until - time.monotonic() <= 45


def test_transient_error_is_not_cached(usage_probe):
    """A network blip should retry on the next poll, not stick around for the
    whole TTL the way a good reading does."""
    import usage as usage_module

    profile, probe = usage_probe
    probe.error = "urlopen error timed out"

    first = usage_module.fetch_claude_usage_for(profile)
    assert first.error == "urlopen error timed out"

    second = usage_module.fetch_claude_usage_for(profile)
    assert probe.calls == 2  # retried immediately rather than serving the failure
    assert usage_module._claude_cache[str(profile.config_dir)].usage is None


def test_render_usage_shows_stale_marker():
    from rich.text import Text
    from usage import ToolUsage, UsageWindow

    app = AdashApp()
    app.usages = [
        ToolUsage(tool="claude", plan="Max", windows=[UsageWindow("5h", 97.0)],
                  stale="rate limited · retry 2m"),
    ]
    row = Text.from_markup(app._usage_text()).plain.splitlines()[1]
    assert "97%" in row  # the bars stay put
    assert "rate limited · retry 2m" in row


def test_stale_marker_never_wraps_the_usage_grid():
    """The bars are the point; when the marker won't fit beside them it gives
    way rather than pushing the row onto a second line. (The 3-column grid is
    ~91 cols on its own, so the bar to clear is the grid, not the terminal.)"""
    from rich.text import Text
    from usage import ToolUsage, UsageWindow

    windows = [UsageWindow("5h", 97.0), UsageWindow("7d", 4.0), UsageWindow("fable", 12.0)]
    app = AdashApp()

    def row_at(width: int | None, stale: str) -> str:
        app.usages = [ToolUsage(tool="claude", plan="Max", stale=stale, windows=windows)]
        return Text.from_markup(app._usage_text(width)).plain.splitlines()[1]

    for width in (80, 100, 117, 160):
        bare, marked = row_at(width, ""), row_at(width, "rate limited · retry 2m")
        assert "97%" in marked  # the grid survives at every width
        # the marker either fits inside the terminal or isn't drawn at all
        assert len(marked) <= max(width, len(bare)), f"marker widened the row at {width} cols"

    assert "rate limited" not in row_at(100, "rate limited · retry 2m")  # no room
    assert "rate limited" in row_at(160, "rate limited · retry 2m")  # plenty


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
    monkeypatch.setattr(app_module, "collect_usage",
                        lambda active_claude=None, force=False: (two_claude, datetime.now(timezone.utc)))
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


# ---------------------------------------------------------------- worktree filter (item 2)


def test_is_worktree_matches_codex_and_claude_paths():
    assert app_module._is_worktree("/Users/x/.codex/worktrees/1e5a/prototype-ehr")
    assert app_module._is_worktree("/Users/x/Repos/web-app/.claude/worktrees/foo")
    assert not app_module._is_worktree("/Users/x/Repos/web-app")


def test_recent_dirs_excludes_worktrees(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    app = AdashApp()
    app.sessions = [
        Session(tool="claude", id="a", title="t", project_dir="/tmp/real-repo", last_active=now),
        Session(tool="codex", id="b", title="t",
                project_dir="/Users/x/.codex/worktrees/abc/web-app", last_active=now),
    ]
    paths = [d[0] for d in app._recent_dirs()]
    assert "/tmp/real-repo" in paths
    assert not any("/worktrees/" in p for p in paths)  # worktrees never offered


# ---------------------------------------------------------------- open another directory (item 3)


def test_other_directory_row_launches_typed_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from textual.widgets import Input

    opened = _launch_harness(tmp_path, monkeypatch)
    target = tmp_path / "some-project"
    target.mkdir()

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 40)) as pilot:
            await _wait_for_table(pilot, app)
            # walk down past every dir onto the "open another directory" row
            for _ in range(len(app.dirs) + 1):
                await pilot.press("down")
            await pilot.pause(0.05)
            assert app.zone == "picker" and app.picker_idx == len(app.dirs)
            await pilot.press("enter")  # opens the path prompt
            await pilot.pause(0.05)
            dirinput = app.query_one("#dirinput", Input)
            assert dirinput.display
            dirinput.value = str(target)
            await pilot.press("enter")  # submit the typed path
            await pilot.pause(0.1)
            assert not dirinput.display  # prompt closes after submit

    asyncio.run(run())
    slug = app_module._dir_slug(str(target))
    assert opened == [["open", f"warp://tab_config/agentdash-claude-{slug}"]]


def test_other_directory_rejects_missing_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from textual.widgets import Input

    opened = _launch_harness(tmp_path, monkeypatch)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 40)) as pilot:
            await _wait_for_table(pilot, app)
            app._open_dir_input()
            await pilot.pause(0.05)
            dirinput = app.query_one("#dirinput", Input)
            dirinput.value = str(tmp_path / "does-not-exist")
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert not dirinput.display

    asyncio.run(run())
    assert opened == []  # nothing launched for a bad path


# ---------------------------------------------------------------- warp tab focus loop (item 4)


def test_focus_warp_tab_stops_after_one_rotation(monkeypatch: pytest.MonkeyPatch):
    # three tabs, none matching the hint; the active tab wraps around the set
    titles = ["◆ Agent Dash", "✳ tab two", "✳ tab three"]
    state = {"i": 0}
    calls = []

    def fake_osa(script: str) -> tuple[bool, str]:
        calls.append(script)
        if "keystroke" in script:
            state["i"] += 1
            return True, ""
        if "activate" in script:
            return True, ""
        return True, titles[state["i"] % len(titles)]

    monkeypatch.setattr(app_module, "_osa", fake_osa)
    result = app_module._focus_warp_tab(["no-such-tab"])
    assert result == "activated"  # gave up cleanly, tab not found
    # one full rotation only: never presses more than there are tabs (old code
    # blindly pressed a fixed 16 times, spinning past every tab 3+ times)
    assert sum("keystroke" in c for c in calls) == len(titles)


# ---------------------------------------------------------------- aligned usage grid (item 1)


def test_window_cell_has_fixed_visible_width():
    from rich.text import Text
    from usage import UsageWindow

    present = Text.from_markup(app_module._window_cell(UsageWindow("5h", 42.0))).plain
    absent = Text.from_markup(app_module._window_cell(None)).plain
    assert len(present) == app_module.USAGE_CELL_W
    assert len(absent) == app_module.USAGE_CELL_W  # missing windows still align


def test_render_usage_aligns_columns_and_firstparty_opencode():
    from rich.text import Text
    from usage import ToolUsage, UsageWindow

    app = AdashApp()
    app.usages = [
        ToolUsage(tool="claude", plan="Team", windows=[UsageWindow("5h", 16.0), UsageWindow("7d", 23.0)]),
        ToolUsage(tool="codex", plan="Pro", windows=[UsageWindow("7d", 100.0)]),
        ToolUsage(tool="opencode", plan="pay-as-you-go", spend=13.06, spend_sessions=5, spend_days=7),
    ]
    plain = Text.from_markup(app._usage_text()).plain
    assert "5h" in plain and "7d" in plain  # single header labels the columns
    assert "pay-as-you-go" in plain and "$13.06" in plain  # opencode as first-party
    assert "no subscription" not in plain  # the old afterthought framing is gone
    # the "5h" header sits directly above where the 5h bars start
    header, first_row = plain.splitlines()[0], plain.splitlines()[1]
    assert header.index("5h") == first_row.index("█")


def test_render_usage_adds_fable_column_only_where_present():
    from rich.text import Text
    from usage import ToolUsage, UsageWindow

    app = AdashApp()
    app.usages = [
        ToolUsage(tool="claude", plan="Team", label="default", active=True,
                  windows=[UsageWindow("5h", 39.0), UsageWindow("7d", 26.0), UsageWindow("fable", 32.0)]),
        ToolUsage(tool="codex", plan="Pro", windows=[UsageWindow("7d", 100.0)]),
    ]
    lines = Text.from_markup(app._usage_text()).plain.splitlines()
    header, claude_row, codex_row = lines[0], lines[1], lines[2]
    assert "fable" in header  # third column labeled once
    assert "32%" in claude_row  # fable percent shown on the claude row
    assert "32%" not in codex_row  # codex has no fable limit -> no third cell
    # the "fable" header sits directly above the claude row's third bar
    fcol = header.index("fable")
    assert claude_row[fcol] in "█░"
