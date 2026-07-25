"""Tests for adash collectors, resume commands, and the TUI flow."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_module
import collectors
from app import AdashApp
from models import Session, resume_command, resume_directory, resume_invocation
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


@pytest.fixture
def gemini_root(tmp_path: Path) -> Path:
    root = tmp_path / "gemini"
    root.mkdir(parents=True, exist_ok=True)
    # projects.json maps a project path to the temp-dir slug; the collector
    # inverts it to resolve a session's cwd from its <slug> directory.
    with (root / "projects.json").open("w") as fh:
        json.dump({"projects": {"/tmp/gem": "proj-slug"}}, fh)
    sid = "11111111-2222-3333-4444-555555555555"
    _write_jsonl(
        root / "tmp" / "proj-slug" / "chats" / "session-1784500000000-11111111.jsonl",
        [
            {"sessionId": sid, "projectHash": "deadbeef", "startTime": "2026-07-19T10:00:00.000Z"},
            {"id": "m1", "type": "user", "content": "add gemini to agent dash", "timestamp": "2026-07-19T10:00:01.000Z"},
            {"id": "m2", "type": "gemini", "content": "Sure, here is the plan.", "timestamp": "2026-07-19T10:00:02.000Z"},
            {"id": "m3", "type": "user", "content": "now write tests too", "timestamp": "2026-07-19T10:00:03.000Z"},
            {"id": "m4", "type": "info", "content": "context loaded", "timestamp": "2026-07-19T10:00:04.000Z"},
        ],
    )
    return root


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


def test_codex_collector_includes_archived_sessions(codex_root: Path):
    archived_id = "aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _write_jsonl(
        codex_root / "archived_sessions" / f"rollout-{archived_id}.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "session_id": archived_id,
                    "cwd": "/tmp/archived",
                    "thread_source": "user",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "keep archived conversations visible",
                },
            },
        ],
    )

    sessions = collectors.collect_codex(root=codex_root)

    assert {session.id for session in sessions} == {
        "1111-2222-3333-4444-555555555555",
        archived_id,
    }
    archived = next(session for session in sessions if session.id == archived_id)
    assert archived.title == "Keep archived conversations visible"


def test_codex_collector_titles_current_user_message_events(tmp_path: Path):
    """Codex 0.145 writes the typed prompt in event_msg, not response_item."""
    root = tmp_path / "codex"
    sid = "aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _write_jsonl(
        root / "sessions" / "2026" / "07" / "21" / f"rollout-{sid}.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": sid, "cwd": "/tmp/cx", "thread_source": "user"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}]}},
            {"type": "event_msg", "payload": {"type": "user_message",
             "message": "[Image #1] can you figure out why codex titles are missing?"}},
        ],
    )

    sessions = collectors.collect_codex(root=root)

    assert len(sessions) == 1
    assert sessions[0].first_prompt == "[Image #1] can you figure out why codex titles are missing?"
    assert sessions[0].title == "[Image #1] can you figure out why codex titles are missing?"


def test_opencode_collector(opencode_db: Path):
    sessions = collectors.collect_opencode(db_path=opencode_db)
    assert len(sessions) == 1  # archived session excluded
    s = sessions[0]
    assert s.id == "ses_1"
    assert s.tokens == 150
    assert s.cost == 0.5
    assert s.project_dir == "/tmp/oc"


def test_gemini_collector(gemini_root: Path):
    sessions = collectors.collect_gemini(root=gemini_root)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.tool == "gemini"
    assert s.id == "11111111-2222-3333-4444-555555555555"  # from the metadata line
    assert s.project_dir == "/tmp/gem"  # resolved via projects.json
    assert s.first_prompt == "add gemini to agent dash"
    assert s.title == "Add gemini to agent dash"  # clean_title of the first prompt
    assert s.n_messages == 3  # 2 user + 1 gemini; the info message is excluded
    assert s.source_path.endswith("session-1784500000000-11111111.jsonl")


def test_gemini_collector_blank_dir_when_project_unmapped(tmp_path: Path):
    """A session in a temp dir with no projects.json entry still parses; its
    project dir is left blank (renders '?', resume falls back to home)."""
    root = tmp_path / "gemini"
    _write_jsonl(
        root / "tmp" / "orphan-slug" / "chats" / "session-1784500000000-99999999.jsonl",
        [
            {"sessionId": "99999999-0000-0000-0000-000000000000"},
            {"id": "m1", "type": "user", "content": "hello gemini", "timestamp": "2026-07-19T10:00:01.000Z"},
        ],
    )
    sessions = collectors.collect_gemini(root=root)
    assert len(sessions) == 1
    assert sessions[0].project_dir == ""
    assert sessions[0].first_prompt == "hello gemini"


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


def test_resume_invocation_has_no_cd_and_directory_falls_home(tmp_path: Path):
    # The Warp resume tab sets the directory itself, so the invocation must be
    # the bare tool command with no `cd`.
    here = Session(tool="claude", id="abc", title="t", project_dir=str(tmp_path),
                   last_active=datetime.now(timezone.utc))
    assert resume_invocation(here) == "claude --resume abc"
    assert resume_directory(here) == str(tmp_path)

    gone = Session(tool="opencode", id="s1", title="t", project_dir="/does/not/exist",
                   last_active=datetime.now(timezone.utc))
    assert resume_invocation(gone) == "opencode --session s1"
    assert resume_directory(gone) == str(Path.home())


def test_gemini_resume_invocation(tmp_path: Path):
    s = Session(tool="gemini", id="g-123", title="t", project_dir=str(tmp_path),
                last_active=datetime.now(timezone.utc))
    assert resume_invocation(s) == "gemini --resume g-123"
    assert resume_command(s) == f"cd {tmp_path} && gemini --resume g-123"


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
    cmd = tomllib.loads(config)["panes"][0]["commands"][0]
    # codex runs with its own title silenced, behind a backgrounded title daemon
    # that gets killed when codex exits (see _codex_launch_command)
    assert "codex -c 'tui.terminal_title=[]'" in cmd
    assert f"--codex-titles --cwd {os.getcwd()}" in cmd
    assert cmd.strip().endswith("; kill $_adtw 2>/dev/null")
    assert "status" not in cmd  # the old status|repo title is gone
    assert f'directory = "{os.getcwd()}"' in config


def test_app_new_gemini_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
            await pilot.press("g")  # gemini launches immediately in the cwd
            await pilot.pause(0.1)

    asyncio.run(run())
    slug = app_module._dir_slug(os.getcwd())
    assert opened == [["open", f"warp://tab_config/agentdash-gemini-{slug}"]]
    config = (tmp_path / "tab_configs" / f"agentdash-gemini-{slug}.toml").read_text()
    assert 'commands = ["gemini"]' in config
    assert f'directory = "{os.getcwd()}"' in config


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


def test_parse_ps_keeps_one_codex_agent_per_terminal():
    from agents import _parse_ps

    ps_output = """\
 60200 ttys007      03:45 node /Users/josephtutera/.nvm/versions/node/v24.14.1/bin/codex
 60201 ttys007      03:45 /Users/josephtutera/.nvm/versions/node/v24.14.1/lib/node_modules/@openai/codex/vendor/bin/codex
"""

    agents = _parse_ps(ps_output)

    assert [(agent.tool, agent.pid, agent.tty) for agent in agents] == [("codex", 60200, "ttys007")]


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


def test_claude_session_id_read_from_pid_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Claude Code writes ~/.claude/sessions/<pid>.json naming the live transcript;
    # reading it is what lets a running agent resolve to its exact session.
    import agents as agents_module

    monkeypatch.setenv("HOME", str(tmp_path))
    sessions_dir = tmp_path / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "56817.json").write_text(json.dumps(
        {"pid": 56817, "sessionId": "4b5d8bef-52fc", "status": "busy"}))
    assert agents_module._claude_session_id_for_pid(56817) == "4b5d8bef-52fc"
    assert agents_module._claude_session_id_for_pid(99999) == ""  # no file for this pid
    (sessions_dir / "42.json").write_text("not json{")
    assert agents_module._claude_session_id_for_pid(42) == ""  # malformed, tolerated


def test_running_agents_tags_claude_with_its_exact_session_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # end-to-end guard for the mislabel bug: a running claude agent must carry
    # its real session id, not just its directory, so recency can't override it.
    import agents as agents_module

    monkeypatch.setenv("HOME", str(tmp_path))
    sessions_dir = tmp_path / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "56817.json").write_text(json.dumps({"sessionId": "sess-4b5d"}))

    class _Result:
        def __init__(self, stdout: str):
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        if cmd[0] == "ps":
            return _Result("56817 ttys003 05:00 claude\n")
        if cmd[0] == "lsof":
            return _Result("p56817\nn/Users/josephtutera/Repos/agent-dash\n")
        return _Result("")

    monkeypatch.setattr(agents_module.subprocess, "run", fake_run)
    agents = agents_module.running_agents()
    assert len(agents) == 1
    assert agents[0].tool == "claude"
    assert agents[0].session_id == "sess-4b5d"
    assert agents[0].cwd == "/Users/josephtutera/Repos/agent-dash"


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
    """Wait for the first history load. The old screen had a DataTable to poll;
    the Bridge layout has one list, so the signal is the entry list filling up."""
    for _ in range(100):
        await pilot.pause(0.05)
        if app.entries:
            break


def _panel_text(app, selector: str) -> str:
    """Plain text of a rendered panel, with the markup tags stripped."""
    from rich.text import Text

    content = app.query_one(selector).content
    plain = getattr(content, "plain", None)
    return plain if isinstance(plain, str) else Text.from_markup(str(content)).plain


def _session_rows(app) -> list:
    """The past-session entries of the single list, in display order."""
    return [e.session for e in app.entries if e.kind == "session"]


def _cursor_session(app):
    entry = app.current
    return entry.session if entry and entry.kind == "session" else None


def test_app_filter_and_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch)
    monkeypatch.setattr(app_module, "_warp_configs_dir", lambda: tmp_path / "tab_configs")
    cmd_file = tmp_path / "cmd"
    opened: list = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            opened.append(args)

    monkeypatch.setattr(app_module, "Popen", FakePopen)

    async def run() -> None:
        app = AdashApp(cmd_file=str(cmd_file))
        async with app.run_test(size=(120, 32)) as pilot:
            await _wait_for_table(pilot, app)
            assert len(_session_rows(app)) == 2

            await pilot.press("3")  # filter to codex only
            await pilot.pause(0.1)
            assert len(_session_rows(app)) == 1

            await pilot.press("1")  # back to all
            await pilot.pause(0.1)
            assert len(_session_rows(app)) == 2

            # one list, one cursor: with nothing running it opens on the newest
            # session, so Enter resumes without any navigation at all
            assert app.mode == "browse"
            assert _cursor_session(app).id == "abc"
            await pilot.press("enter")
            await pilot.pause(0.1)

    asyncio.run(run())
    # Resume opens a fresh Warp tab running the resume command, not an in-place
    # shell command written back to the terminal.
    assert opened == [["open", "warp://tab_config/agentdash-claude-resume-abc"]]
    config = (tmp_path / "tab_configs" / "agentdash-claude-resume-abc.toml").read_text()
    assert 'commands = ["claude --resume abc"]' in config
    assert 'directory = "/tmp"' in config
    assert not cmd_file.exists()  # nothing written back to the shell

def test_selection_starts_on_claude_and_navigates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The tool chips moved into the launcher, so left/right only steer once you
    have asked for a new session. In the list they do nothing, which is what
    stops the old four-zone chain from creeping back."""
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(120, 32)) as pilot:
            await _wait_for_table(pilot, app)
            assert app.mode == "browse"

            await pilot.press("right")  # no-op while browsing
            await pilot.pause(0.05)
            assert app.mode == "browse" and app.launch_idx == 0

            await pilot.press("n")
            await pilot.pause(0.05)
            assert app.mode == "launcher"
            assert app.launch_idx == 0  # starts on claude

            await pilot.press("right")
            await pilot.pause(0.05)
            assert app.launch_idx == 1  # codex

            await pilot.press("right")
            await pilot.pause(0.05)
            assert app.launch_idx == 2  # opencode

            await pilot.press("left")
            await pilot.pause(0.05)
            assert app.launch_idx == 1

            await pilot.press("escape")  # back to the list
            await pilot.pause(0.05)
            assert app.mode == "browse"

    asyncio.run(run())

def test_navigation_through_active_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """One cursor walks straight from the running agents into history. The old
    screen made you cross four zones to get here."""
    from agents import RunningAgent

    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch, agents=[
        RunningAgent(tool="claude", pid=1, tty="ttys001", elapsed="5m", cwd="/nowhere-1"),
        RunningAgent(tool="codex", pid=2, tty="ttys002", elapsed="9m", cwd="/nowhere-2"),
    ])

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(120, 32)) as pilot:
            await _wait_for_table(pilot, app)
            for _ in range(100):
                await pilot.pause(0.05)
                if len(app.running) == 2:
                    break

            kinds = [e.kind for e in app.entries]
            assert kinds[:2] == ["agent", "agent"]  # live work sorts to the top
            assert "session" in kinds

            assert app.cursor == 0
            await pilot.press("down")
            await pilot.pause(0.05)
            assert app.cursor == 1 and app.current.kind == "agent"

            await pilot.press("down")
            await pilot.pause(0.05)
            assert app.cursor == 2 and app.current.kind == "session"

            await pilot.press("up")
            await pilot.pause(0.05)
            assert app.cursor == 1 and app.current.kind == "agent"

            for _ in range(5):  # the cursor stops at the top, it does not wrap
                await pilot.press("up")
            await pilot.pause(0.05)
            assert app.cursor == 0

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
        async with app.run_test(size=(120, 32)) as pilot:
            await _wait_for_table(pilot, app)
            for _ in range(100):
                await pilot.pause(0.05)
                if app.running:
                    break
            # the running agent is the first row, so Enter needs no navigation
            assert app.current.kind == "agent"
            await pilot.press("enter")
            await _wait_for_focus_call(pilot, calls)

    asyncio.run(run())
    assert calls and "web-app" in calls[0]

def test_click_active_row_selects_then_jumps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A click selects; a second click on the same row commits it.

    The old panel jumped on the first click. Now that one list holds live agents
    and resumable history together, a stray click would have spawned a Warp tab,
    so the first click only moves the cursor and lets the detail pane tell you
    what the next one will do.
    """
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
        async with app.run_test(size=(120, 32)) as pilot:
            await _wait_for_table(pilot, app)
            for _ in range(100):
                await pilot.pause(0.05)
                if len(app.running) == 2:
                    break
            second = next(y for y, i in sorted(app._line_index.items()) if i == 1)

            await pilot.click("#listbody", offset=(5, second))
            await pilot.pause(0.1)
            assert app.cursor == 1  # selected, and nothing was launched
            assert not calls

            await pilot.click("#listbody", offset=(5, second))
            await _wait_for_focus_call(pilot, calls)
            assert calls and "api-server" in calls[0]

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

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(120, 32)) as pilot:
            await _wait_for_table(pilot, app)
            assert len(_session_rows(app)) == 3

            await pilot.press("C")  # clear: hide everything seen so far
            await pilot.pause(0.1)
            assert len(_session_rows(app)) == 0
            # the toast fades, so the header must keep advertising the way back
            assert "U restores" in _panel_text(app, "#header")

            await pilot.press("U")  # undo
            await pilot.pause(0.1)
            assert len(_session_rows(app)) == 3
            assert "U restores" not in _panel_text(app, "#header")

    asyncio.run(run())

def test_clear_history_does_not_persist_across_launches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # An accidental C used to write a marker to disk and reload it on every
    # launch, hiding history forever. Clearing must be scoped to the live run:
    # a fresh app always starts with the full history visible.
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: _fake_sessions())
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch)

    async def run() -> None:
        first = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with first.run_test(size=(110, 32)) as pilot:
            await _wait_for_table(pilot, first)
            await pilot.press("C")
            await pilot.pause(0.1)
            assert first.cleared_at is not None

        # a brand-new instance (as if adash were relaunched) is never cleared
        second = AdashApp(cmd_file=str(tmp_path / "cmd"))
        assert second.cleared_at is None
        async with second.run_test(size=(110, 32)) as pilot:
            await _wait_for_table(pilot, second)
            assert len(_session_rows(second)) == 2

    asyncio.run(run())


def test_history_auto_refresh_shows_new_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # the session list used to load once at startup; a session started (or the
    # one you're in) never appeared without a manual r. It must poll and update.
    monkeypatch.setattr(app_module, "HISTORY_POLL_SECONDS", 0.2)
    state = {"sessions": _fake_sessions()}
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: list(state["sessions"]))
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(110, 32)) as pilot:
            await _wait_for_table(pilot, app)
            assert len(_session_rows(app)) == 2

            newer = Session(tool="claude", id="new", title="Brand new", project_dir="/tmp",
                            last_active=datetime.now(timezone.utc), tokens=1, n_messages=1,
                            first_prompt="just started")
            state["sessions"] = [newer] + _fake_sessions()
            for _ in range(60):
                await pilot.pause(0.05)
                if len(_session_rows(app)) == 3:
                    break
            assert len(_session_rows(app)) == 3

    asyncio.run(run())


def test_history_refresh_preserves_selected_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The active poll rebuilds the single list every couple of seconds. If a new
    # session lands on top it must not drag the cursor onto a different row —
    # the cursor is keyed by identity, not by index, precisely for this.
    monkeypatch.setattr(app_module, "HISTORY_POLL_SECONDS", 0.2)
    state = {"sessions": _fake_sessions()}  # [abc (claude), xyz (codex)]
    monkeypatch.setattr(app_module, "collect_all", lambda limit=300: list(state["sessions"]))
    _stub_usage(monkeypatch)
    _stub_running(monkeypatch)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(120, 32)) as pilot:
            await _wait_for_table(pilot, app)
            await pilot.press("down")  # take hold of the cursor: now it is yours
            await pilot.pause(0.05)
            assert _cursor_session(app).id == "xyz"

            newer = Session(tool="claude", id="new", title="Brand new", project_dir="/tmp",
                            last_active=datetime.now(timezone.utc), tokens=1, n_messages=1,
                            first_prompt="just started")
            state["sessions"] = [newer] + _fake_sessions()
            for _ in range(60):
                await pilot.pause(0.05)
                if len(_session_rows(app)) == 3:
                    break
            assert len(_session_rows(app)) == 3
            assert _cursor_session(app).id == "xyz"  # still on the same session

    asyncio.run(run())

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
    assert app_module.LAUNCH_TOOLS == ("claude", "codex", "opencode", "gemini", "terminal")


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
    cmd = tomllib.loads(config)["panes"][0]["commands"][0]
    assert "codex -c 'tui.terminal_title=[]'" in cmd


def test_codex_tab_config_silences_codex_title_and_runs_the_daemon(tmp_path: Path):
    """A fresh codex tab silences codex's own generic title (terminal_title=[])
    and runs the in-tab title daemon that gives it a Claude-style descriptive
    title. The generated TOML must survive a parse round-trip."""
    app_module._write_tab_config("codex", "/tmp/proj", tmp_path)
    config = (tmp_path / "agentdash-codex.toml").read_text()
    cmd = tomllib.loads(config)["panes"][0]["commands"][0]
    assert "codex -c 'tui.terminal_title=[]'" in cmd
    assert "--codex-titles --cwd /tmp/proj" in cmd
    assert cmd.strip().endswith("; kill $_adtw 2>/dev/null")


def test_codex_resume_passes_the_session_to_the_daemon(tmp_path: Path):
    """Resuming a codex session hands its id to the title daemon (so it tracks
    that exact session) and still silences codex's own title before the `resume`
    subcommand."""
    app_module._write_tab_config("codex", "/tmp/proj", tmp_path, suffix="resume-abc",
                                 command="codex resume abc123")
    config = (tmp_path / "agentdash-codex-resume-abc.toml").read_text()
    cmd = tomllib.loads(config)["panes"][0]["commands"][0]
    assert "--codex-titles --cwd /tmp/proj --session abc123" in cmd
    assert "codex -c 'tui.terminal_title=[]' resume abc123" in cmd


def test_non_codex_tabs_are_left_alone(tmp_path: Path):
    """Only codex gets the override; claude titles its own tab, and injecting a
    codex flag into other tools would break their launch."""
    for tool, expected in (("claude", "claude"), ("opencode", "opencode")):
        app_module._write_tab_config(tool, "/tmp/proj", tmp_path)
        config = (tmp_path / f"agentdash-{tool}.toml").read_text()
        cmd = tomllib.loads(config)["panes"][0]["commands"][0]
        assert cmd == expected


def test_bar_color_ramp():
    # calm blue below 60, amber in the warning band, red when critical.
    # The full-width bar went with the permanent usage panel; the ramp itself
    # still drives the mini bars in the `u` overlay.
    assert "#7aa2f7" in app_module._usage_bar(41.0)
    assert "#ffb454" in app_module._usage_bar(62.0)
    assert "#ff5c57" in app_module._usage_bar(100.0)


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
    """Enter opens exactly the highlighted directory and leaves the launcher up,
    so several tabs is several presses rather than a checkbox multi-select."""
    opened = _launch_harness(tmp_path, monkeypatch)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_table(pilot, app)
            assert len(app.dirs) == 2  # cwd + /tmp
            await pilot.press("n")
            await pilot.pause(0.05)
            assert app.picker_idx == 0  # cwd preselected
            await pilot.press("enter")  # opens cwd only
            await pilot.pause(0.1)
            assert len(opened) == 1
            assert app.mode == "launcher"  # stays up for the next one
            await pilot.press("down")   # -> dirs[1] (/tmp)
            await pilot.press("enter")
            await pilot.pause(0.1)
            # the pane echoes both directories opened this run
            assert app.picker_opened == [os.getcwd(), "/tmp"]

    asyncio.run(run())
    assert len(opened) == 2  # cwd + /tmp
    stems = sorted(o[1].split("/")[-1] for o in opened)
    assert stems == sorted([f"agentdash-claude-{app_module._dir_slug(os.getcwd())}", "agentdash-claude-tmp"])

def test_picker_launches_selected_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    opened = _launch_harness(tmp_path, monkeypatch)

    async def run() -> None:
        app = AdashApp(cmd_file=str(tmp_path / "cmd"))
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_table(pilot, app)
            await pilot.press("n")      # open the launcher
            await pilot.press("right")  # chips: claude -> codex
            await pilot.pause(0.05)
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
    creds = _profile_credentials(ClaudeProfile(label="personal", config_dir=d))
    assert creds.token == "tok-xyz"
    assert creds.plan == "Max 20X"
    assert creds.file_path == d / ".credentials.json"  # write refreshes back here


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
    from usage import ClaudeCredentials, ClaudeProfile, ToolUsage, UsageWindow

    usage_module._claude_cache.clear()

    class Probe:
        calls = 0
        fail: Exception | None = None  # set to RateLimited/Unauthorized to raise on the next call
        fail_calls: int | None = None  # how many calls `fail` applies to; None means every one
        error: str | None = None  # set to simulate a transient (non-429) failure
        pct = 40.0
        refreshes = 0
        refresh_ok = True  # flip off to simulate a dead/revoked refresh token
        expires_in = 3600.0  # seconds from now on the stored access token
        tokens: list[str] = []  # every access token the endpoint was called with

    probe = Probe()
    probe.tokens = []

    def fake_fetch(token: str, plan: str) -> ToolUsage:
        probe.calls += 1
        probe.tokens.append(token)
        if probe.fail is not None and probe.fail_calls != 0:
            if probe.fail_calls is not None:
                probe.fail_calls -= 1
            raise probe.fail
        if probe.error is not None:
            return ToolUsage(tool="claude", plan=plan, error=probe.error)
        return ToolUsage(tool="claude", plan=plan, windows=[UsageWindow("5h", probe.pct)])

    def fake_creds(profile):
        return ClaudeCredentials(
            oauth={"accessToken": "tok", "refreshToken": "r", "subscriptionType": "max",
                   "expiresAt": int((time.time() + probe.expires_in) * 1000)},
            keychain_service="svc")

    def fake_refresh(creds):
        probe.refreshes += 1
        if not probe.refresh_ok:
            return None
        return ClaudeCredentials(
            oauth={**creds.oauth, "accessToken": "tok-refreshed",
                   "expiresAt": int((time.time() + 28800) * 1000)},
            keychain_service=creds.keychain_service)

    monkeypatch.setattr(usage_module, "_profile_credentials", fake_creds)
    monkeypatch.setattr(usage_module, "_refresh_claude_token", fake_refresh)
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


def test_expired_token_is_refreshed_before_the_call(usage_probe):
    """The bug behind the blank plan cards: Claude Code only mints a new access
    token when it makes its own API call, so an account left idle overnight has
    a dead token sitting in the Keychain. Sending it is a guaranteed 401."""
    import usage as usage_module

    profile, probe = usage_probe
    probe.expires_in = -86400  # expired a day ago, like the personal account was

    usage = usage_module.fetch_claude_usage_for(profile)

    assert probe.refreshes == 1
    assert probe.tokens == ["tok-refreshed"]  # the dead one was never sent
    assert usage.windows[0].pct == 40.0 and not usage.error


def test_401_refreshes_once_and_retries(usage_probe):
    """A live-looking token can still be refused (revoked, or rotated by another
    tool). One refresh, one retry -- not a silent failure."""
    import usage as usage_module

    profile, probe = usage_probe
    probe.fail, probe.fail_calls = usage_module.Unauthorized(), 1  # refused once, then fine

    usage = usage_module.fetch_claude_usage_for(profile)

    assert probe.refreshes == 1
    assert probe.tokens == ["tok", "tok-refreshed"]
    assert usage.windows[0].pct == 40.0 and not usage.error


def test_dead_credential_backs_off_instead_of_hammering(usage_probe):
    """What turned a fixable auth problem into an hour-long 429 lockout: a 401
    was treated as transient, so the daemon re-fired every poll forever. A
    credential that needs a human can't heal on the next poll."""
    import usage as usage_module

    profile, probe = usage_probe
    probe.fail = usage_module.Unauthorized()
    probe.refresh_ok = False  # the refresh token is dead too

    first = usage_module.fetch_claude_usage_for(profile)
    assert "claude auth login" in first.error  # says what to actually do

    calls_before, refreshes_before = probe.calls, probe.refreshes
    for _ in range(5):
        again = usage_module.fetch_claude_usage_for(profile, force=True)
    assert probe.calls == calls_before  # never touched the endpoint again
    assert probe.refreshes == refreshes_before
    assert "claude auth login" in again.error

    entry = usage_module._claude_cache[str(profile.config_dir)]
    assert entry.cooldown_until - time.monotonic() > usage_module.USAGE_CACHE_TTL_SECONDS
    assert usage_module.AUTH_FAILURE_COOLDOWN_SECONDS >= 600


def test_dead_credential_keeps_the_last_known_bars(usage_probe):
    """Losing auth shouldn't blank a card that had a real number a minute ago."""
    import usage as usage_module

    profile, probe = usage_probe
    usage_module.fetch_claude_usage_for(profile)  # a good reading first
    probe.fail = usage_module.Unauthorized()
    probe.refresh_ok = False

    degraded = usage_module.fetch_claude_usage_for(profile, force=True)
    assert degraded.windows[0].pct == 40.0
    assert "claude auth login" in degraded.stale
    assert degraded.error is None


def test_transient_error_keeps_the_last_known_bars(usage_probe):
    """A network blip is not a reason to throw away a good reading."""
    import usage as usage_module

    profile, probe = usage_probe
    usage_module.fetch_claude_usage_for(profile)
    probe.error = "urlopen error timed out"

    degraded = usage_module.fetch_claude_usage_for(profile, force=True)
    assert degraded.windows[0].pct == 40.0
    assert "timed out" in degraded.stale


def test_recovering_from_a_dead_credential_clears_the_cooldown(usage_probe):
    import usage as usage_module

    profile, probe = usage_probe
    probe.fail = usage_module.Unauthorized()
    probe.refresh_ok = False
    usage_module.fetch_claude_usage_for(profile)

    entry = usage_module._claude_cache[str(profile.config_dir)]
    entry.cooldown_until = 0.0  # as if the cooldown lapsed
    probe.fail, probe.refresh_ok = None, True

    recovered = usage_module.fetch_claude_usage_for(profile, force=True)
    assert recovered.windows[0].pct == 40.0
    assert not recovered.stale and entry.cooldown_until == 0.0


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
        return usage_module.ClaudeCredentials(
            oauth={"accessToken": "tok-personal", "subscriptionType": "claude_max_20x"},
            keychain_service=service)

    monkeypatch.setattr(usage_module, "_keychain_claude_credentials", fake_keychain)
    creds = _profile_credentials(ClaudeProfile(label="personal", config_dir=d))
    assert creds.token == "tok-personal" and creds.plan == "Max 20X"
    assert seen["service"] == usage_module._keychain_service(ClaudeProfile(label="personal", config_dir=d))
    assert creds.keychain_service == seen["service"]  # refreshes write back to the same item


# --------------------------------------------------- oauth token refresh


def _oauth_creds(**over) -> "usage_module.ClaudeCredentials":  # type: ignore[name-defined]
    import usage as usage_module

    oauth = {
        "accessToken": "tok-old",
        "refreshToken": "refresh-old",
        "expiresAt": int((time.time() + 3600) * 1000),
        "subscriptionType": "max",
        "scopes": ["user:inference"],
    }
    oauth.update(over.pop("oauth", {}))
    return usage_module.ClaudeCredentials(oauth=oauth, keychain_service="Claude Code-credentials-abc", **over)


def test_credentials_expiry_uses_a_refresh_skew():
    """A token that dies in the next few minutes is already useless to a poller
    that only wakes every 3 minutes, so it counts as expired."""
    import usage as usage_module

    live = _oauth_creds()
    assert live.expired() is False

    dying = _oauth_creds(oauth={"expiresAt": int((time.time() + 60) * 1000)})
    assert dying.expired() is True  # inside the skew

    dead = _oauth_creds(oauth={"expiresAt": int((time.time() - 86400) * 1000)})
    assert dead.expired() is True

    # an entry with no expiry recorded can't be judged; assume it's usable
    unknown = _oauth_creds(oauth={"expiresAt": None})
    assert unknown.expired() is False
    assert usage_module.TOKEN_REFRESH_SKEW_SECONDS > 0


def test_refresh_persists_rotated_credentials(monkeypatch: pytest.MonkeyPatch):
    """The server may hand back a new refresh token. Keeping it only in memory
    would leave Claude Code holding a dead one, so it must be written back."""
    import usage as usage_module

    posted = {}

    def fake_post(url, body, timeout=10):
        posted["url"], posted["body"] = url, body
        return {
            "access_token": "tok-new",
            "refresh_token": "refresh-new",
            "expires_in": 28800,
            "refresh_token_expires_in": 2592000,
        }

    saved = {}
    monkeypatch.setattr(usage_module, "_post_json", fake_post)
    monkeypatch.setattr(usage_module, "_persist_credentials", lambda c: saved.setdefault("oauth", c.oauth) or True)

    fresh = usage_module._refresh_claude_token(_oauth_creds(oauth={"expiresAt": 0}))

    assert posted["url"] == usage_module.CLAUDE_OAUTH_TOKEN_URL
    assert posted["body"]["grant_type"] == "refresh_token"
    assert posted["body"]["refresh_token"] == "refresh-old"
    assert posted["body"]["client_id"] == usage_module.CLAUDE_OAUTH_CLIENT_ID

    assert fresh.token == "tok-new"
    assert fresh.oauth["refreshToken"] == "refresh-new"  # rotation taken up
    assert fresh.expired() is False
    assert saved["oauth"]["refreshToken"] == "refresh-new"  # ...and written back
    assert saved["oauth"]["subscriptionType"] == "max"  # untouched fields survive


def test_refresh_keeps_old_refresh_token_when_server_omits_one(monkeypatch: pytest.MonkeyPatch):
    import usage as usage_module

    monkeypatch.setattr(usage_module, "_post_json",
                        lambda url, body, timeout=10: {"access_token": "tok-new", "expires_in": 3600})
    monkeypatch.setattr(usage_module, "_persist_credentials", lambda c: True)

    fresh = usage_module._refresh_claude_token(_oauth_creds())
    assert fresh.oauth["refreshToken"] == "refresh-old"


def test_refresh_returns_none_when_the_grant_is_refused(monkeypatch: pytest.MonkeyPatch):
    import usage as usage_module

    monkeypatch.setattr(usage_module, "_post_json", lambda url, body, timeout=10: None)
    persisted = []
    monkeypatch.setattr(usage_module, "_persist_credentials", lambda c: persisted.append(c))

    assert usage_module._refresh_claude_token(_oauth_creds()) is None
    assert persisted == []  # nothing to write when the refresh failed


def test_keychain_write_round_trips_a_quoted_payload(monkeypatch: pytest.MonkeyPatch):
    """The blob is JSON, so it is full of the quotes and backslashes that
    `security -i` treats as syntax. Verify what we hand the tool is escaped."""
    import usage as usage_module

    captured = {}

    class Result:
        returncode = 0

    def fake_run(argv, **kw):
        captured["argv"], captured["input"] = argv, kw.get("input")
        return Result()

    monkeypatch.setattr(usage_module.subprocess, "run", fake_run)
    creds = usage_module.ClaudeCredentials(
        oauth={"accessToken": 'a"b\\c'}, keychain_service="Claude Code-credentials-abc")
    assert usage_module._persist_credentials(creds) is True

    assert captured["argv"][:2] == ["security", "-i"]  # payload over stdin, never argv
    assert 'a"b\\c' not in captured["argv"]
    line = captured["input"]
    assert "add-generic-password -U" in line and "Claude Code-credentials-abc" in line
    assert '\\"' in line and "\\\\" in line  # quotes and backslashes escaped for the tool


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
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_table(pilot, app)
            await pilot.press("n")
            # walk down past every dir onto the "type a path" row
            for _ in range(len(app.dirs)):
                await pilot.press("down")
            await pilot.pause(0.05)
            assert app.mode == "launcher" and app.picker_idx == len(app.dirs)
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


def test_render_usage_hides_windows_no_tool_reports():
    from rich.text import Text
    from usage import ToolUsage, UsageWindow

    app = AdashApp()
    app.usages = [ToolUsage(tool="codex", plan="Pro", windows=[UsageWindow("7d", 0.0)])]

    plain = Text.from_markup(app._usage_text()).plain

    assert "7d" in plain
    assert "5h" not in plain


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


# ---------------------------------------------------------------- value at API rates

import pricing  # noqa: E402


# A tiny hand-built price table: rates in whole dollars per token so the pricing
# math is trivial to check by hand (1 input token = $1, etc.).
_TEST_TABLE = {
    "claude-opus-4-8": {
        "input_cost_per_token": 1.0,
        "output_cost_per_token": 2.0,
        "cache_creation_input_token_cost": 0.5,
        "cache_read_input_token_cost": 0.1,
        "litellm_provider": "anthropic",
    },
    # a model missing both cache keys, to exercise the fallback rules
    "gpt-5.6": {
        "input_cost_per_token": 10.0,
        "output_cost_per_token": 20.0,
        "litellm_provider": "openai",
    },
}


def _resolver() -> "pricing.PriceResolver":
    return pricing.PriceResolver(_TEST_TABLE)


def test_pricing_all_four_token_buckets():
    """input/output/cache_creation/cache_read each priced at its own rate."""
    resolver = _resolver()
    rates = resolver.rates_for("claude-opus-4-8")
    bucket = pricing._Buckets(input=100, output=10, cache_creation=40, cache_read=1000)
    # 100*1 + 10*2 + 40*0.5 + 1000*0.1 = 100 + 20 + 20 + 100 = 240
    assert bucket.cost(rates) == 240.0


def test_pricing_cache_rate_fallbacks_when_table_omits_them():
    """Missing cache keys fall back to input rate (creation) and 0.1x (read)."""
    rates = _resolver().rates_for("gpt-5.6")
    assert rates.cache_creation == 10.0  # == input rate
    assert rates.cache_read == 1.0  # == 0.1 * input rate


def test_model_alias_resolution_strips_prefix_and_suffix():
    resolver = _resolver()
    # provider prefix stripped
    assert resolver.rates_for("anthropic/claude-opus-4-8").input == 1.0
    # codex internal alias peels down to gpt-5.6
    assert resolver.rates_for("gpt-5.6-terra").input == 10.0
    # a dated id still finds the undated table entry
    assert resolver.rates_for("gpt-5.6-2025-12-01").input == 10.0
    assert not resolver.skipped  # everything above resolved


def test_skipped_models_are_reported_not_priced_at_zero():
    resolver = _resolver()
    assert resolver.rates_for("some-unknown-model-9000") is None
    assert resolver.rates_for("") is None
    assert "some-unknown-model-9000" in resolver.skipped
    assert "(unknown)" in resolver.skipped


def _write_claude_assistant(root: Path, project: str, name: str, ts: str, model: str, usage: dict, cost=None):
    line = {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "model": model, "usage": usage}}
    if cost is not None:
        line["costUSD"] = cost
    _write_jsonl(root / project / name, [line])


def test_claude_scanner_prices_tokens_and_attributes_by_day(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(pricing, "load_price_table", lambda **kw: _TEST_TABLE)
    monkeypatch.setattr(pricing, "load_subscription_costs", lambda home=None: {})
    home = tmp_path
    projects = home / ".claude" / "projects"
    # two assistant turns on the same local day, plus one earlier this month
    _write_claude_assistant(
        projects, "-proj", "s1.jsonl", "2026-07-21T18:00:00+00:00", "claude-opus-4-8",
        {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 1000},
    )
    _write_claude_assistant(
        projects, "-proj", "s2.jsonl", "2026-07-05T12:00:00+00:00", "claude-opus-4-8",
        {"input_tokens": 10, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    now = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)
    report = pricing.collect_value(now=now, home=home, cache_dir=tmp_path / "cache")

    team = report.sub("claude-team")
    assert team is not None
    assert team.today_usd == 240.0  # only the July 21 turn
    assert team.month_usd == 250.0  # July 21 ($240) + July 5 ($10)


def test_costusd_passthrough_beats_token_math(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(pricing, "load_price_table", lambda **kw: _TEST_TABLE)
    monkeypatch.setattr(pricing, "load_subscription_costs", lambda home=None: {})
    home = tmp_path
    projects = home / ".claude" / "projects"
    # this line would price to $240 from tokens, but its explicit costUSD wins
    _write_claude_assistant(
        projects, "-proj", "s1.jsonl", "2026-07-21T18:00:00+00:00", "claude-opus-4-8",
        {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 1000},
        cost=3.5,
    )
    now = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)
    report = pricing.collect_value(now=now, home=home, cache_dir=tmp_path / "cache")
    assert report.sub("claude-team").today_usd == 3.5


def test_scanner_reports_skipped_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(pricing, "load_price_table", lambda **kw: _TEST_TABLE)
    monkeypatch.setattr(pricing, "load_subscription_costs", lambda home=None: {})
    home = tmp_path
    projects = home / ".claude" / "projects"
    _write_claude_assistant(
        projects, "-proj", "s1.jsonl", "2026-07-21T18:00:00+00:00", "no-such-model",
        {"input_tokens": 100, "output_tokens": 10},
    )
    now = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)
    report = pricing.collect_value(now=now, home=home, cache_dir=tmp_path / "cache")
    assert report.sub("claude-team").today_usd == 0.0  # unpriceable, not free-at-zero silently
    assert "no-such-model" in report.skipped_models


def test_scan_cache_invalidates_on_mtime_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(pricing, "load_price_table", lambda **kw: _TEST_TABLE)
    monkeypatch.setattr(pricing, "load_subscription_costs", lambda home=None: {})
    home = tmp_path
    cache_dir = tmp_path / "cache"
    projects = home / ".claude" / "projects"
    f = projects / "-proj" / "s1.jsonl"
    _write_claude_assistant(
        projects, "-proj", "s1.jsonl", "2026-07-21T18:00:00+00:00", "claude-opus-4-8",
        {"input_tokens": 100, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    now = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)
    first = pricing.collect_value(now=now, home=home, cache_dir=cache_dir)
    assert first.sub("claude-team").today_usd == 100.0

    # rewrite the file with different content and a newer mtime; the stale cache
    # entry must be dropped and the new tokens re-priced
    time.sleep(0.01)
    _write_claude_assistant(
        projects, "-proj", "s1.jsonl", "2026-07-21T18:00:00+00:00", "claude-opus-4-8",
        {"input_tokens": 250, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    os.utime(f, (time.time() + 5, time.time() + 5))
    second = pricing.collect_value(now=now, home=home, cache_dir=cache_dir)
    assert second.sub("claude-team").today_usd == 250.0


def _write_codex_rollout(root: Path, rel: str, sid: str, thread_source: str, model: str,
                         turns: list[tuple[str, dict]]):
    lines = [{"type": "session_meta", "timestamp": turns[0][0] if turns else "2026-07-21T00:00:00Z",
              "payload": {"session_id": sid, "id": sid, "cwd": "/tmp/cx", "thread_source": thread_source}}]
    if model:
        lines.append({"type": "turn_context", "payload": {"type": "turn_context", "model": model}})
    for ts, last in turns:
        lines.append({"type": "event_msg", "timestamp": ts,
                      "payload": {"type": "token_count", "info": {"last_token_usage": last}}})
    _write_jsonl(root / "sessions" / rel, lines)


def test_codex_scanner_prices_last_token_usage_and_skips_subagents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(pricing, "load_price_table", lambda **kw: _TEST_TABLE)
    monkeypatch.setattr(pricing, "load_subscription_costs", lambda home=None: {})
    home = tmp_path
    codex = home / ".codex"
    # a user session: input_tokens includes the cached slice, so non-cached = 100-40 = 60
    _write_codex_rollout(
        codex, "2026/07/21/rollout-a.jsonl", "sid-a", "user", "gpt-5.6-terra",
        [("2026-07-21T18:00:00+00:00",
          {"input_tokens": 100, "cached_input_tokens": 40, "cache_write_input_tokens": 5, "output_tokens": 10})],
    )
    # a subagent rollout for a different sid: must be skipped entirely
    _write_codex_rollout(
        codex, "2026/07/21/rollout-b.jsonl", "sid-b", "subagent", "gpt-5.6",
        [("2026-07-21T18:30:00+00:00",
          {"input_tokens": 1000, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 1000})],
    )
    now = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)
    report = pricing.collect_value(now=now, home=home, cache_dir=tmp_path / "cache")
    codex_sub = report.sub("codex")
    # gpt-5.6 fallbacks: creation = input rate 10, read = 1
    # 60*10 (non-cached input) + 10*20 (output) + 5*10 (creation) + 40*1 (read) = 600+200+50+40 = 890
    assert codex_sub.today_usd == 890.0  # subagent tokens excluded


def test_today_and_month_boundary_attribution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A turn on the last day of the previous month is neither today nor this month."""
    monkeypatch.setattr(pricing, "load_price_table", lambda **kw: _TEST_TABLE)
    monkeypatch.setattr(pricing, "load_subscription_costs", lambda home=None: {})
    home = tmp_path
    projects = home / ".claude" / "projects"
    _write_claude_assistant(
        projects, "-proj", "june.jsonl", "2026-06-30T18:00:00+00:00", "claude-opus-4-8",
        {"input_tokens": 100, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    _write_claude_assistant(
        projects, "-proj", "julyfirst.jsonl", "2026-07-01T09:00:00+00:00", "claude-opus-4-8",
        {"input_tokens": 50, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    now = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)
    report = pricing.collect_value(now=now, home=home, cache_dir=tmp_path / "cache")
    team = report.sub("claude-team")
    assert team.today_usd == 0.0  # nothing today
    assert team.month_usd == 50.0  # June 30 excluded, July 1 included


def test_config_parsing_and_multiple(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if pricing.tomllib is None:
        pytest.skip("tomllib unavailable on this interpreter")
    monkeypatch.setattr(pricing, "load_price_table", lambda **kw: _TEST_TABLE)
    home = tmp_path
    projects = home / ".claude" / "projects"
    _write_claude_assistant(
        projects, "-proj", "s.jsonl", "2026-07-10T18:00:00+00:00", "claude-opus-4-8",
        {"input_tokens": 300, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    cfg = home / ".config" / "adash"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text(
        "[subscriptions]\n"
        'claude-team = 150.0\n'
        'codex = 200\n'
    )
    now = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)
    report = pricing.collect_value(now=now, home=home, cache_dir=tmp_path / "cache")
    team = report.sub("claude-team")
    assert team.subs_cost_usd == 150.0
    assert team.month_usd == 300.0
    assert team.multiple == 2.0  # $300 of value over a $150 sub
    assert report.subs_cost_usd == 350.0  # 150 + 200
    assert report.multiple == 300.0 / 350.0


def test_config_missing_leaves_costs_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(pricing, "load_price_table", lambda **kw: _TEST_TABLE)
    now = datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)
    report = pricing.collect_value(now=now, home=tmp_path, cache_dir=tmp_path / "cache")
    assert report.subs_cost_usd is None
    assert report.multiple is None
    for sub in report.subs:
        assert sub.subs_cost_usd is None and sub.multiple is None


def test_price_table_falls_back_to_snapshot_on_network_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """No cache and a dead network still yields the committed snapshot."""
    monkeypatch.setattr(pricing, "_fetch_live_prices", lambda timeout=8.0: None)
    table = pricing.load_price_table(cache_dir=tmp_path / "empty-cache")
    assert "claude-opus-4-8" in table  # came from the bundled snapshot


def test_price_table_prefers_fresh_cache_over_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "model-prices.json").write_text(json.dumps({"cached-model": {"input_cost_per_token": 1.0}}))
    called = {"n": 0}

    def fake_fetch(timeout=8.0):
        called["n"] += 1
        return {"live-model": {"input_cost_per_token": 2.0}}

    monkeypatch.setattr(pricing, "_fetch_live_prices", fake_fetch)
    table = pricing.load_price_table(cache_dir=cache_dir)  # fresh cache (just written)
    assert "cached-model" in table
    assert called["n"] == 0  # never hit the network within the 24h window


def test_snapshot_file_is_present_and_parseable():
    """The committed offline fallback must exist and contain the models we price."""
    snap = pricing._read_json(pricing._snapshot_path())
    assert snap is not None
    assert "claude-opus-4-8" in snap and "gpt-5.6" in snap
    rates = pricing._rates_from_entry(snap["claude-opus-4-8"])
    assert rates is not None and rates.input > 0


def test_hud_value_is_json_safe_and_matches_contract():
    """The daemon consumes hud_value(); it must be plain json.dumps-able and carry
    exactly the contract keys (no set, no datetime leaking through)."""
    report = pricing.ValueReport(
        subs=[
            pricing.SubValue(id="claude-team", today_usd=8.334, month_usd=15209.651,
                             subs_cost_usd=150.0, multiple=101.4),
            pricing.SubValue(id="codex", today_usd=0.0, month_usd=3774.544),
        ],
        today_total_usd=8.334,
        month_total_usd=18984.201,
        subs_cost_usd=150.0,
        multiple=126.561,
        skipped_models={"<synthetic>"},  # a set: plain json.dumps would reject the raw report
        generated_at=datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc),  # a datetime: likewise
    )
    hud = pricing.hud_value(report)

    # serializes with the stdlib encoder, no custom default= needed
    encoded = json.dumps(hud)
    assert isinstance(encoded, str)

    # exactly the contract keys at the top level
    assert set(hud.keys()) == {"today_usd", "month_usd", "subs_cost_usd", "multiple", "by_sub"}
    # dropped: the CLI-only fields never reach the daemon
    assert "skipped_models" not in hud and "generated_at" not in hud

    # dollars rounded to cents
    assert hud["today_usd"] == 8.33 and hud["month_usd"] == 18984.2
    assert hud["subs_cost_usd"] == 150.0

    # by_sub keyed by subscription id, each with exactly today/month
    assert set(hud["by_sub"].keys()) == {"claude-team", "codex"}
    assert hud["by_sub"]["claude-team"] == {"today_usd": 8.33, "month_usd": 15209.65}
    assert set(hud["by_sub"]["codex"].keys()) == {"today_usd", "month_usd"}


def test_hud_value_carries_none_cost_and_multiple_through():
    report = pricing.ValueReport(
        subs=[pricing.SubValue(id="codex", today_usd=1.0, month_usd=2.0)],
        today_total_usd=1.0, month_total_usd=2.0,
        subs_cost_usd=None, multiple=None,
    )
    hud = pricing.hud_value(report)
    assert hud["subs_cost_usd"] is None and hud["multiple"] is None
    json.dumps(hud)  # None is JSON-safe


# ---------------------------------------------------------------- picker: cwd pin


def _dir_sessions() -> list[Session]:
    """Two history sessions in ordinary project dirs, proj-b more recent."""
    now = datetime.now(timezone.utc)
    return [
        Session(tool="claude", id="a", title="t", project_dir="/tmp/proj-a",
                last_active=now.replace(microsecond=1)),
        Session(tool="claude", id="b", title="t", project_dir="/tmp/proj-b",
                last_active=now.replace(microsecond=2)),
    ]


def test_recent_dirs_pins_cwd_when_not_a_worktree():
    # The autouse conftest chdir's every test into a neutral (non-worktree) dir,
    # so the current directory is a legitimate place to start a fresh session.
    stub = SimpleNamespace(sessions=_dir_sessions())
    dirs = [d for d, _count, _last in AdashApp._recent_dirs(stub)]
    assert dirs[0] == os.getcwd()  # cwd pinned to the top
    assert set(dirs[1:]) == {"/tmp/proj-a", "/tmp/proj-b"}


def test_recent_dirs_does_not_pin_cwd_inside_a_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # You never start a fresh session in a throwaway agent worktree, so when the
    # picker itself is launched from inside one, cwd must not be pinned (mirroring
    # how history already excludes worktree dirs). This is the regression guard.
    worktree = tmp_path / ".claude" / "worktrees" / "foo"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(worktree)

    stub = SimpleNamespace(sessions=_dir_sessions())
    dirs = [d for d, _count, _last in AdashApp._recent_dirs(stub)]
    assert str(worktree) not in dirs  # the worktree cwd is not offered
    assert dirs == ["/tmp/proj-b", "/tmp/proj-a"]  # ranked history dirs, unpinned


# ------------------------------------------------- codex in-tab title daemon

import tab_titles  # noqa: E402
from activity import LiveStatus, codex_status_for_file  # noqa: E402
from tab_titles import (  # noqa: E402
    CodexTabTitler,
    compose_tab_title,
    resolve_codex_session,
)
from titles import TitleStore, cached_or_fallback_title  # noqa: E402


def test_codex_launch_command_wraps_fresh_and_resume():
    fresh = app_module._codex_launch_command("codex", "/tmp/cx", "")
    # daemon backgrounded first, codex (title silenced) in the foreground, then
    # the daemon is killed when codex returns
    assert "--codex-titles --cwd /tmp/cx" in fresh
    assert "--session" not in fresh
    assert " & _adtw=$!; " in fresh
    assert "codex -c 'tui.terminal_title=[]'" in fresh
    assert fresh.endswith("; kill $_adtw 2>/dev/null")

    resume = app_module._codex_launch_command("codex resume abc-123", "/tmp/cx", "")
    assert "--session abc-123" in resume
    assert "codex -c 'tui.terminal_title=[]' resume abc-123" in resume


def test_codex_launch_command_env_prefixes_codex_not_the_daemon():
    cmd = app_module._codex_launch_command("codex", "/tmp/cx", "FOO=bar ")
    daemon, _, foreground = cmd.partition(" & _adtw=$!; ")
    assert "FOO=bar" not in daemon  # the daemon is not the thing being configured
    assert foreground.startswith("FOO=bar codex -c 'tui.terminal_title=[]'")


def test_resolve_codex_session_matches_resume_id(codex_root: Path):
    sid = "1111-2222-3333-4444-555555555555"
    session = resolve_codex_session("/tmp/cx", session_id=sid, root=codex_root)
    assert session is not None and session.id == sid


def test_resolve_codex_session_finds_fresh_session_by_directory(codex_root: Path):
    session = resolve_codex_session("/tmp/cx", session_id="", since=0.0, root=codex_root)
    assert session is not None and session.project_dir == "/tmp/cx"
    # a session that started well before we launched is not this tab's session
    future = time.time() + 10_000
    assert resolve_codex_session("/tmp/cx", since=future, root=codex_root) is None
    # nor is a session in a different directory
    assert resolve_codex_session("/tmp/other", since=0.0, root=codex_root) is None


def test_compose_tab_title_shows_spinner_only_while_working():
    assert compose_tab_title("Fix The Bug", working=True, frame_char="⠙") == "⠙ Fix The Bug"
    assert compose_tab_title("Fix The Bug", working=False, frame_char="⠙") == "Fix The Bug"
    # no title yet: never emit a lone spinner
    assert compose_tab_title("", working=True, frame_char="⠙") == ""


def test_cached_or_fallback_title_prefers_the_generated_title(tmp_path: Path):
    store = TitleStore(cache_dir=tmp_path)
    session = Session(tool="codex", id="s1", title="Bug Fix Thread",
                      project_dir="/tmp/cx", last_active=datetime.now(timezone.utc),
                      first_prompt="fix the bug", n_messages=4)
    # with nothing cached, fall back to the collector's title
    assert cached_or_fallback_title(session, store) == "Bug Fix Thread"
    store.put("s1", "Generated Title", 4, "codex")
    assert cached_or_fallback_title(session, store) == "Generated Title"


def test_codex_status_for_file_reports_working_then_idle(tmp_path: Path):
    working = tmp_path / "working.jsonl"
    _write_jsonl(working, [
        {"type": "session_meta", "payload": {"session_id": "w", "cwd": "/tmp/cx", "thread_source": "user"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
    ])
    assert codex_status_for_file(working).state == "working"

    idle = tmp_path / "idle.jsonl"
    _write_jsonl(idle, [
        {"type": "session_meta", "payload": {"session_id": "i", "cwd": "/tmp/cx", "thread_source": "user"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ])
    assert codex_status_for_file(idle).state == "idle"


def test_codex_tab_titler_generates_a_title_and_animates_when_working(codex_root: Path, tmp_path: Path):
    store = TitleStore(cache_dir=tmp_path)
    state = {"working": True}
    titler = CodexTabTitler(
        "/tmp/cx", session_id="", store=store, codex_root=codex_root, since=0.0,
        generate=lambda session: "Generated Title",
        spawn=lambda fn: fn(),  # run generation synchronously for a deterministic test
        status_for_file=lambda path: LiveStatus(state="working" if state["working"] else "idle"),
    )
    # first tick: resolves the session, generates + caches a title, sees "working"
    first = titler.tick(now=0.0)
    assert first == "⠙ Generated Title"  # spinner frame 1 + generated title
    assert store.get(titler.session.id)["title"] == "Generated Title"  # shared cache

    # once codex goes idle the spinner drops and the bare title remains
    state["working"] = False
    assert titler.tick(now=10.0) == "Generated Title"


def test_codex_tab_titler_shows_directory_placeholder_before_the_session_exists(tmp_path: Path):
    empty_root = tmp_path / "empty-codex"
    (empty_root / "sessions").mkdir(parents=True)
    titler = CodexTabTitler("/tmp/web-app", session_id="", codex_root=empty_root,
                            store=TitleStore(cache_dir=tmp_path))
    assert titler.tick(now=0.0) == "codex · web-app"


def test_main_dispatches_codex_titles(monkeypatch: pytest.MonkeyPatch):
    import main as main_module

    calls = {}
    monkeypatch.setattr(tab_titles, "run_codex_titles",
                        lambda cwd, session_id="": calls.update(cwd=cwd, session_id=session_id))
    monkeypatch.setattr(sys, "argv", ["adash", "--codex-titles", "--cwd", "/tmp/cx", "--session", "abc"])
    main_module.main()
    assert calls == {"cwd": "/tmp/cx", "session_id": "abc"}
