"""Tests for the generative-title pipeline (titles.py) and the Warp tab push."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import titles
from models import Session, osc_title_sequence
from titles import (
    TitleStore,
    apply_titles,
    conversation_excerpt,
    generate_title,
    needs_title,
    sanitize_title,
)


def _session(tool="codex", sid="s1", n_messages=5, first_prompt="", source_path="") -> Session:
    return Session(
        tool=tool,
        id=sid,
        title="raw first prompt title",
        project_dir="/tmp/proj",
        last_active=datetime(2026, 7, 21, tzinfo=timezone.utc),
        n_messages=n_messages,
        first_prompt=first_prompt,
        source_path=source_path,
    )


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for line in lines:
            fh.write(json.dumps(line, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------- OSC sequence


def test_osc_title_sequence_sets_all_three():
    seq = osc_title_sequence("Hello")
    assert seq == "\x1b]0;Hello\x07\x1b]1;Hello\x07\x1b]2;Hello\x07"


# ---------------------------------------------------------------- TitleStore


def test_title_store_roundtrip(tmp_path: Path):
    store = TitleStore(cache_dir=tmp_path)
    assert store.get("s1") is None
    store.put("s1", "Fix Flaky Checkout Test", 12, "codex")
    # a fresh store re-reads from disk
    reloaded = TitleStore(cache_dir=tmp_path)
    entry = reloaded.get("s1")
    assert entry == {"title": "Fix Flaky Checkout Test", "n_messages": 12, "tool": "codex"}


def test_title_store_discards_wrong_version(tmp_path: Path):
    (tmp_path / "titles.json").write_text(
        json.dumps({"version": 999, "titles": {"s1": {"title": "old"}}})
    )
    store = TitleStore(cache_dir=tmp_path)
    assert store.get("s1") is None


def test_title_store_survives_corrupt_file(tmp_path: Path):
    (tmp_path / "titles.json").write_text("{ not json")
    store = TitleStore(cache_dir=tmp_path)
    assert store.get("s1") is None


# ---------------------------------------------------------------- needs_title


@pytest.mark.parametrize(
    "cached_n, current_n, expected",
    [
        (None, 5, True),    # never titled
        (10, 12, False),    # small growth
        (10, 15, False),    # grew 5, not doubled
        (10, 20, True),     # doubled and grew >= 8
        (10, 10, False),    # unchanged
        (0, 0, False),      # opencode (n_messages stays 0): title once, never again
    ],
)
def test_needs_title(tmp_path: Path, cached_n, current_n, expected):
    store = TitleStore(cache_dir=tmp_path)
    if cached_n is not None:
        store.put("s1", "cached", cached_n, "codex")
    assert needs_title(_session(sid="s1", n_messages=current_n), store) is expected


def test_apply_titles_overlays_and_returns_eligible(tmp_path: Path):
    store = TitleStore(cache_dir=tmp_path)
    store.put("cached", "Cached Title", 5, "codex")
    cached = _session(sid="cached", n_messages=6)      # has title, small growth -> not eligible
    fresh = _session(sid="fresh", n_messages=3)        # never titled -> eligible
    eligible = apply_titles([cached, fresh], store)
    assert cached.title == "Cached Title"              # overlaid in place
    assert [s.id for s in eligible] == ["fresh"]


def test_apply_titles_keeps_stale_title_while_regenerating(tmp_path: Path):
    store = TitleStore(cache_dir=tmp_path)
    store.put("s1", "Old But Cached", 5, "codex")
    grown = _session(sid="s1", n_messages=20)          # doubled -> eligible
    eligible = apply_titles([grown], store)
    assert grown.title == "Old But Cached"             # still shows the cached title
    assert [s.id for s in eligible] == ["s1"]          # and is queued for a refresh


# ---------------------------------------------------------------- sanitize


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('"Fix Flaky Checkout Test"', "Fix Flaky Checkout Test"),
        ("Title: Warp Tab Configs", "Warp Tab Configs"),
        ("Add Regression Tests.", "Add Regression Tests"),
        ("preamble line\nActual Title Here", "Actual Title Here"),
        ("  Spaced   Out  Title  ", "Spaced Out Title"),
    ],
)
def test_sanitize_title(raw, expected):
    assert sanitize_title(raw) == expected


def test_sanitize_title_clamps_long_output():
    out = sanitize_title("word " * 40)
    assert len(out) <= 61 and out.endswith("…")


# ---------------------------------------------------------------- excerpts


def test_excerpt_claude(tmp_path: Path):
    path = tmp_path / "sess.jsonl"
    _write_jsonl(
        path,
        [
            {"type": "user", "message": {"role": "user", "content": "fix the flaky checkout test"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "internal reasoning, not shown"},
                {"type": "text", "text": "I'll look at the retry logic."},
            ]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "also add a regression test"}]}},
        ],
    )
    excerpt = conversation_excerpt(_session(tool="claude", source_path=str(path)))
    assert "fix the flaky checkout test" in excerpt
    assert "I'll look at the retry logic." in excerpt
    assert "also add a regression test" in excerpt
    assert "internal reasoning" not in excerpt  # thinking blocks skipped


def test_excerpt_codex(tmp_path: Path):
    path = tmp_path / "rollout-cx1.jsonl"
    _write_jsonl(
        path,
        [
            {"type": "session_meta", "payload": {"session_id": "cx1", "cwd": "/tmp", "thread_source": "user"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "set up warp tab configs"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Sure, I'll create the toml files."}]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "make them colored"}},
        ],
    )
    excerpt = conversation_excerpt(_session(tool="codex", source_path=str(path)))
    assert "set up warp tab configs" in excerpt
    assert "Sure, I'll create the toml files." in excerpt
    assert "make them colored" in excerpt


def test_excerpt_opencode(tmp_path: Path):
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE message (id text PRIMARY KEY, session_id text, time_created integer, data text)")
    con.execute("CREATE TABLE part (id text PRIMARY KEY, message_id text, session_id text, time_created integer, data text)")
    con.execute("INSERT INTO message VALUES ('m1','oc1',1,?)", (json.dumps({"role": "user"}),))
    con.execute("INSERT INTO message VALUES ('m2','oc1',2,?)", (json.dumps({"role": "assistant"}),))
    con.execute("INSERT INTO message VALUES ('m3','oc1',3,?)", (json.dumps({"role": "user"}),))
    con.execute("INSERT INTO part VALUES ('p1','m1','oc1',1,?)", (json.dumps({"type": "text", "text": "can you set up open router"}),))
    con.execute("INSERT INTO part VALUES ('p2','m2','oc1',2,?)", (json.dumps({"type": "text", "text": "Yes, here is how"}),))
    con.execute("INSERT INTO part VALUES ('p2r','m2','oc1',3,?)", (json.dumps({"type": "reasoning", "text": "hidden reasoning"}),))
    con.execute("INSERT INTO part VALUES ('p3','m3','oc1',4,?)", (json.dumps({"type": "text", "text": "thanks that works"}),))
    con.commit()
    con.close()
    excerpt = conversation_excerpt(_session(tool="opencode", sid="oc1", source_path=str(db)))
    assert "can you set up open router" in excerpt
    assert "Yes, here is how" in excerpt
    assert "thanks that works" in excerpt
    assert "hidden reasoning" not in excerpt  # non-text parts skipped


def test_excerpt_missing_source_returns_empty():
    assert conversation_excerpt(_session(tool="claude", source_path="")) == ""
    assert conversation_excerpt(_session(tool="claude", source_path="/nope/gone.jsonl")) == ""


# ---------------------------------------------------------------- generation


def test_generate_title_uses_codex_output(tmp_path: Path, monkeypatch):
    path = tmp_path / "sess.jsonl"
    _write_jsonl(path, [{"type": "user", "message": {"role": "user", "content": "fix the flaky checkout test"}}])
    monkeypatch.setattr(titles, "_run_codex_titler", lambda prompt, **kw: '"Fix Flaky Checkout Test"')
    title = generate_title(_session(tool="claude", source_path=str(path)))
    assert title == "Fix Flaky Checkout Test"


def test_generate_title_falls_back_when_codex_fails(tmp_path: Path, monkeypatch):
    path = tmp_path / "sess.jsonl"
    _write_jsonl(path, [{"type": "user", "message": {"role": "user", "content": "hello"}}])
    monkeypatch.setattr(titles, "_run_codex_titler", lambda prompt, **kw: None)
    session = _session(tool="claude", source_path=str(path), first_prompt="please fix the flaky test")
    assert generate_title(session) == "Fix the flaky test"


def test_codex_titler_builds_expected_argv(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        out_path = argv[argv.index("-o") + 1]
        Path(out_path).write_text("My Generated Title\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(titles.subprocess, "run", fake_run)
    result = titles._run_codex_titler("prompt goes here")
    argv = captured["argv"]
    assert argv[:2] == ["codex", "exec"]
    assert "--ephemeral" in argv
    assert "--ignore-user-config" in argv
    assert argv[argv.index("-m") + 1] == "gpt-5.6-luna"
    assert "model_reasoning_effort=none" in argv
    assert "-o" in argv
    # regression guard: no inherited stdin (breaks codex under the TUI) and a
    # neutral cwd (so it doesn't load the host repo's AGENTS.md into the turn)
    assert captured["kwargs"]["stdin"] == titles.subprocess.DEVNULL
    assert captured["kwargs"]["cwd"]
    assert result == "My Generated Title"


def test_codex_titler_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(
        titles.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    assert titles._run_codex_titler("prompt") is None
