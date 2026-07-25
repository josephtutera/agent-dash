"""Tests for reading a session's conversation back as turns.

The parsers matter less than the three rules around them: never read a whole
transcript (they reach hundreds of megabytes), never show injected scaffolding
as if someone said it, and never let one malformed line lose the rest.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import bridge
import transcript
from models import Session

NOW = datetime.now(timezone.utc)


def _session(tool: str, path: Path, sid: str = "s1") -> Session:
    return Session(tool, sid, "A session", "/tmp", NOW, source_path=str(path))


@pytest.fixture(autouse=True)
def _clear_cache():
    transcript._cache.clear()
    yield
    transcript._cache.clear()


def _write(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records))
    return path


# ------------------------------------------------------------------- claude

def test_claude_turns_alternate_in_order(tmp_path: Path):
    path = _write(tmp_path / "c.jsonl", [
        {"message": {"role": "user", "content": "first ask"}},
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "first reply"}]}},
        {"message": {"role": "user", "content": "second ask"}},
    ])
    turns = transcript.read_turns(_session("claude", path))
    assert [(t.role, t.text) for t in turns] == [
        ("user", "first ask"), ("agent", "first reply"), ("user", "second ask")
    ]


def test_injected_scaffolding_is_not_shown_as_something_you_said(tmp_path: Path):
    """Command wrappers and system reminders are in the transcript but were
    never typed by anyone."""
    path = _write(tmp_path / "c.jsonl", [
        {"message": {"role": "user", "content": "<command-name>/model</command-name>"}},
        {"message": {"role": "user", "content": "<system-reminder>be good</system-reminder>"}},
        {"message": {"role": "user", "content": "Caveat: the messages below were generated"}},
        {"message": {"role": "user", "content": "an actual question"}},
    ])
    turns = transcript.read_turns(_session("claude", path))
    assert [t.text for t in turns] == ["an actual question"]


def test_consecutive_turns_from_one_speaker_are_merged(tmp_path: Path):
    """A single reply arrives as several records; left alone it reads as the
    agent talking to itself."""
    path = _write(tmp_path / "c.jsonl", [
        {"message": {"role": "assistant", "content": "part one"}},
        {"message": {"role": "assistant", "content": "part two"}},
        {"message": {"role": "user", "content": "ok"}},
    ])
    turns = transcript.read_turns(_session("claude", path))
    assert len(turns) == 2
    assert turns[0].text == "part one part two"


def test_a_malformed_line_does_not_lose_the_rest(tmp_path: Path):
    path = tmp_path / "c.jsonl"
    path.write_text('{"message": {"role": "user", "content": "kept"}}\nnot json at all\n'
                    '{"message": {"role": "assistant", "content": "also kept"}}')
    turns = transcript.read_turns(_session("claude", path))
    assert [t.text for t in turns] == ["kept", "also kept"]


def test_whitespace_is_collapsed_so_a_turn_is_one_paragraph(tmp_path: Path):
    path = _write(tmp_path / "c.jsonl", [
        {"message": {"role": "user", "content": "hard\nwrapped   across\n\nlines"}},
    ])
    assert transcript.read_turns(_session("claude", path))[0].text == "hard wrapped across lines"


def test_a_very_long_turn_is_clipped(tmp_path: Path):
    path = _write(tmp_path / "c.jsonl", [
        {"message": {"role": "user", "content": "word " * 5000}},
    ])
    assert len(transcript.read_turns(_session("claude", path))[0].text) <= transcript.MAX_CHARS


# -------------------------------------------------------------------- codex

def test_codex_turns_come_from_both_record_shapes(tmp_path: Path):
    path = _write(tmp_path / "x.jsonl", [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "do the thing"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "text", "text": "done"}]}},
    ])
    turns = transcript.read_turns(_session("codex", path))
    assert [(t.role, t.text) for t in turns] == [("user", "do the thing"), ("agent", "done")]


# ------------------------------------------------------------------- gemini

def test_gemini_turns(tmp_path: Path):
    path = _write(tmp_path / "g.json", [
        {"type": "user", "content": "hello"},
        {"type": "gemini", "content": "hi back"},
    ])
    turns = transcript.read_turns(_session("gemini", path))
    assert [(t.role, t.text) for t in turns] == [("user", "hello"), ("agent", "hi back")]


# ----------------------------------------------------------------- opencode

def test_opencode_turns_come_back_in_chronological_order(tmp_path: Path):
    db = tmp_path / "oc.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT)")
    con.execute("CREATE TABLE part (message_id TEXT, session_id TEXT, time_created INT, data TEXT)")
    for i, (mid, role, text) in enumerate([
        ("m1", "user", "first"), ("m2", "assistant", "second"), ("m3", "user", "third"),
    ]):
        con.execute("INSERT INTO message VALUES (?, ?)", (mid, json.dumps({"role": role})))
        con.execute("INSERT INTO part VALUES (?, ?, ?, ?)",
                    (mid, "s1", i, json.dumps({"type": "text", "text": text})))
    con.commit(); con.close()
    turns = transcript.read_turns(_session("opencode", db))
    assert [t.text for t in turns] == ["first", "second", "third"]


# -------------------------------------------------------------------- bounds

def test_only_the_tail_of_a_huge_transcript_is_read(tmp_path: Path):
    """A long codex session runs to hundreds of megabytes. Reading all of it on
    every cursor move would stall the arrow keys."""
    path = tmp_path / "big.jsonl"
    filler = json.dumps({"message": {"role": "user", "content": "x" * 500}})
    with path.open("w") as fh:
        for _ in range(4000):  # comfortably past TAIL_BYTES
            fh.write(filler + "\n")
        fh.write(json.dumps({"message": {"role": "assistant", "content": "the last word"}}) + "\n")
    assert path.stat().st_size > transcript.TAIL_BYTES
    turns = transcript.read_turns(_session("claude", path))
    assert turns[-1].text == "the last word"
    assert len(turns) <= transcript.MAX_TURNS


def test_results_are_cached_until_the_file_changes(tmp_path: Path):
    path = _write(tmp_path / "c.jsonl", [{"message": {"role": "user", "content": "one"}}])
    session = _session("claude", path)
    assert len(transcript.read_turns(session)) == 1
    assert len(transcript._cache) == 1

    path.unlink()  # a cache hit must not need the file
    assert len(transcript.read_turns(session)) == 0  # stat fails, so nothing to show


def test_a_missing_or_pathless_session_yields_nothing(tmp_path: Path):
    assert transcript.read_turns(Session("claude", "x", "t", "/tmp", NOW)) == []
    assert transcript.read_turns(_session("claude", tmp_path / "gone.jsonl")) == []


# ------------------------------------------------------------------ rendering

def test_the_block_reads_as_scrollback(tmp_path: Path):
    """Speaker on its own line, body indented under it, oldest first."""
    turns = [transcript.Turn("user", "the ask"), transcript.Turn("agent", "the reply")]
    lines = bridge.transcript_block(turns, bridge.DARK, 40, "codex").splitlines()
    text = [__import__("rich.text", fromlist=["Text"]).Text.from_markup(l).plain for l in lines]
    assert text[0] == "conversation"
    assert text[1] == "you"
    assert text[2].startswith("  the ask")
    assert "codex" in text  # the agent's turns are labelled with the tool
    assert text.index("you") < text.index("codex")  # chronological


def test_an_empty_conversation_renders_nothing_at_all():
    assert bridge.transcript_block([], bridge.DARK) == ""


def test_long_turns_wrap_under_the_indent_not_back_to_the_margin():
    turn = [transcript.Turn("user", "word " * 40)]
    from rich.text import Text

    body = [Text.from_markup(l).plain for l in
            bridge.transcript_block(turn, bridge.DARK, 40).splitlines()[2:]]
    body = [l for l in body if l.strip()]
    assert len(body) > 1
    assert all(l.startswith("  ") for l in body)
    assert all(len(l) <= 42 for l in body)
