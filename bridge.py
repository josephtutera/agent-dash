"""Presentation layer for the two-pane "Bridge" layout.

Everything the redesign decides about *how things look* lives here as pure
functions over the existing data models, so the layout can be unit-tested
without standing up a Textual app. `app.py` owns the widgets and the keyboard;
this module owns colour, wording, and the shape of each pane.

Two rules carry most of the design:

- **Colour means state, never tool identity.** The old screen gave claude,
  codex, opencode and gemini each their own hue, so the whole screen was a
  rainbow and the one thing that actually mattered (an agent is waiting on you)
  had no way to stand out. Here green means running fine, amber means it wants
  you, cobalt means selection, and everything else is grey. The tool is a word
  in the meta line.
- **The detail pane always names the action.** Whatever is highlighted on the
  left, the right pane ends with a sentence describing exactly what Enter will
  do, so the app teaches itself instead of having to be memorised.

Sizes from the Paper mock don't survive into a terminal, which has one font at
one size. The type scale collapses onto what a terminal actually has: bold for
the thing that matters, dim for support, and UPPERCASE + wide tracking for
section labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich.markup import escape

from models import Session, fmt_tokens, rel_time, resume_invocation

# ----------------------------------------------------------------- the palette

@dataclass(frozen=True)
class Theme:
    """One colourway. Nine roles, identical names in both modes, so switching
    themes is a single dict swap and no other code changes."""

    name: str
    ground: str  # the screen
    raised: str  # a panel that sits above the screen (command strips)
    selected: str  # the highlighted row
    rule: str  # hairlines and borders
    text: str  # primary
    text_soft: str  # secondary: meta lines, body copy
    text_dim: str  # labels, timestamps, anything you read only on purpose
    accent: str  # selection and the primary action. never a state
    running: str  # an agent working away happily
    waiting: str  # an agent that wants you. the one colour that should pull
    done: str  # finished its turn and has something for you to read

    def on_accent(self) -> str:
        """Text colour for the filled action bar. The accent is dark in light
        mode and mid-bright in dark mode, and white clears both."""
        return "#FFFFFF"


LIGHT = Theme(
    name="light",
    ground="#FFFFFF",
    raised="#F5F7FA",
    selected="#E6EDFB",
    rule="#DCE3EB",
    text="#101418",
    text_soft="#59646F",
    text_dim="#8792A0",
    accent="#1B4FD8",
    running="#136B47",
    waiting="#A85C04",
    done="#0E6F8E",
)

DARK = Theme(
    name="dark",
    ground="#0D1014",
    raised="#151A20",
    selected="#1A2434",
    rule="#232C36",
    text="#E8EDF3",
    text_soft="#8894A2",
    text_dim="#5D6874",
    # cobalt, green and amber all lift in dark mode. The light values are tuned
    # for contrast against white and simply vanish against near-black.
    accent="#4C7DF0",
    running="#4FA37F",
    waiting="#D9A040",
    done="#54A8C7",
)

THEMES = {"light": LIGHT, "dark": DARK}
DEFAULT_THEME = "dark"


def css_variables(theme: Theme) -> dict[str, str]:
    """Palette as Textual CSS variables. Fed to `App.get_css_variables`, which
    means a theme switch is `self.theme_name = ...; self.refresh_css()` and the
    whole stylesheet re-resolves."""
    return {
        "ad-ground": theme.ground,
        "ad-raised": theme.raised,
        "ad-selected": theme.selected,
        "ad-rule": theme.rule,
        "ad-text": theme.text,
        "ad-text-soft": theme.text_soft,
        "ad-text-dim": theme.text_dim,
        "ad-accent": theme.accent,
        "ad-running": theme.running,
        "ad-waiting": theme.waiting,
        "ad-done": theme.done,
    }


# ------------------------------------------------------------------- utilities

LIST_TITLE_W = 38  # the mock truncates list titles here; see the SPEC artboard
SELECTION_BAR = "│"  # the 2px cobalt rule down the left edge of a selected row


def truncate(text: str, width: int) -> str:
    """Clip with an ellipsis, the way the old table did, so a long title can
    never push the elapsed column out of its lane."""
    text = text or ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def label(text: str, theme: Theme, *, strong: bool = False) -> str:
    """A section label. Plain lowercase, quiet.

    This used to fake letter-spacing by padding capitals apart, the way the
    Paper mock renders tracked small caps. A proportional canvas can carry
    that; a terminal cannot, because every gap is a whole cell wide, so it came
    out shouting and hard to read. Weight and colour do the work instead.
    """
    colour = theme.text if strong else theme.text_dim
    body = f"[bold]{text.lower()}[/bold]" if strong else text.lower()
    return f"[{colour}]{body}[/]"


def rule_line(theme: Theme, width: int) -> str:
    return f"[{theme.rule}]{'─' * max(0, width)}[/]"


# --------------------------------------------------------------- state markers

#: Terminal spinners, the character-based equivalent of a spinner library's
#: animated set. All are pure text on the terminal's own grid, so they cost a
#: string index per frame and never fight the font.
SPINNERS = {
    "dots": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    "line": "|/-\\",
    "arc": "◜◠◝◞◡◟",
    "bounce": "⠁⠂⠄⠂",
    "pulse": "∙∘○∘",
    "grow": "▁▃▄▅▆▇▆▅▄▃",
    "toggle": "⊶⊷",
    "arrow": "←↖↑↗→↘↓↙",
    "circle": "◐◓◑◒",
    "square": "◰◳◲◱",
}
DEFAULT_SPINNER = "dots"

#: Four shapes, not four shades of one dot, so the states stay legible without
#: relying on colour vision. `working` is the odd one out: it animates, and the
#: glyph here is only the still frame used when the spinner is not ticking.
#: `unknown` reads like idle on purpose — not having resolved an agent's state
#: yet is the normal case for the first second and shouldn't flash at you.
_STATE_GLYPH = {
    "working": "●",
    "waiting": "◆",
    "done": "●",
    "idle": "○",
    "unknown": "·",
}


def state_colour(state: str, theme: Theme) -> str:
    if state == "working":
        return theme.running
    if state == "waiting":
        return theme.waiting
    if state == "done":
        return theme.done
    if state == "idle":
        return theme.text_soft
    return theme.text_dim


def spinner_frame(tick: int, name: str = DEFAULT_SPINNER) -> str:
    frames = SPINNERS.get(name, SPINNERS[DEFAULT_SPINNER])
    return frames[tick % len(frames)]


def state_marker(state: str, theme: Theme, tick: int | None = None,
                 spinner: str = DEFAULT_SPINNER) -> str:
    """The marker at the head of a running row.

    A working agent animates. That motion is the cheapest possible way to say
    "this is alive right now" and it is the one thing a static dot cannot
    convey — you would otherwise have to read the elapsed column to tell a
    busy agent from a stalled one.
    """
    if state == "working" and tick is not None:
        glyph = spinner_frame(tick, spinner)
    else:
        glyph = _STATE_GLYPH.get(state, "·")
    return f"[{state_colour(state, theme)}]{glyph}[/]"


def state_words(state: str) -> str:
    """Plain-English state for the eyebrow above the detail title."""
    return {
        "working": "working",
        "waiting": "waiting for you",
        "done": "finished its turn",
        "idle": "idle",
    }.get(state, "running")


# ------------------------------------------------------------------- list rows

@dataclass(frozen=True)
class Row:
    """One entry in the left pane: two lines of text plus a fixed-width elapsed
    column. Kept as data rather than markup so tests can assert on the parts."""

    marker: str  # markup for the state dot, or "" for a past session
    title: str  # already truncated to LIST_TITLE_W
    meta: str  # "tool · project · state"
    stamp: str  # right-aligned, 4 cells: "12m", "20h", "2d", "now"
    selected: bool = False


def compact_stamp(text: str) -> str:
    """`rel_time` is written for a wide column ("just now", "20h ago"). The
    left pane only has four cells, so squeeze it."""
    text = (text or "").strip()
    if text in ("just now", "now"):
        return "now"
    return text.removesuffix(" ago")


def agent_row(agent, title: str, *, selected: bool = False, theme: Theme = DARK,
              tick: int | None = None, spinner: str = DEFAULT_SPINNER) -> Row:
    """A live agent. The meta line leads with the tool and ends with what it is
    doing right now, which is the only place the label earns its space."""
    bits = [agent.tool, _short_project(agent.cwd)]
    if agent.state == "waiting":
        bits.append(agent.label or "waiting for your approval")
    elif agent.state == "done":
        bits.append(agent.label or "finished its turn")
    elif agent.label:
        bits.append(agent.label)
    return Row(
        marker=state_marker(agent.state, theme, tick, spinner),
        title=truncate(title or agent.title or _short_project(agent.cwd), LIST_TITLE_W),
        meta=" · ".join(b for b in bits if b),
        stamp=compact_stamp(agent.elapsed),
        selected=selected,
    )


def session_row(session: Session, *, selected: bool = False) -> Row:
    """A past session. No marker: the empty slot is what makes the running rows
    above it read as a distinct group without needing a second heading."""
    meta = f"{session.tool} · {session.project_name}"
    if session.n_messages:
        meta += f" · {session.n_messages} msgs"
    return Row(
        marker="",
        title=truncate(session.title or "(untitled)", LIST_TITLE_W),
        meta=meta,
        stamp=compact_stamp(rel_time(session.last_active)),
        selected=selected,
    )


def render_row(row: Row, theme: Theme, width: int) -> str:
    """Two lines of markup for one row. The elapsed column is right-aligned in a
    fixed four cells so the times form a clean edge no matter how the titles
    wrap; the selection bar lives in column 0 for the same reason."""
    bar = f"[{theme.accent}]{SELECTION_BAR}[/]" if row.selected else " "
    marker = row.marker or " "
    stamp = row.stamp.rjust(4)
    title_w = max(10, width - 12)  # bar + marker + gaps + stamp
    title_colour = theme.text
    title = escape(truncate(row.title, title_w))
    weight = "bold" if row.selected else ""
    head = f"{bar} {marker} [{title_colour}]{f'[{weight}]' if weight else ''}{title}"
    head += f"{f'[/{weight}]' if weight else ''}[/]"
    head = _pad_to(head, 4 + len(row.title), width - 4) + f"[{theme.text_soft}]{stamp}[/]"
    meta_colour = theme.text_soft if row.selected else theme.text_dim
    body = f"    [{meta_colour}]{escape(truncate(row.meta, title_w))}[/]"
    return f"{head}\n{body}"


def _pad_to(markup: str, visible: int, width: int) -> str:
    return markup + " " * max(1, width - visible)


def _short_project(path: str) -> str:
    from pathlib import Path

    return Path(path).name if path else "?"


# ----------------------------------------------------------------- detail pane

def _eyebrow(theme: Theme, colour: str, state: str, facts: str) -> str:
    return (
        f"[{colour}]●[/] [{colour}][bold]{state}[/bold][/]"
        f"  [{theme.text_dim}]{facts}[/]"
    )


def _heading(theme: Theme, text: str) -> str:
    """The mock's 32px title. A terminal has one size, so the weight and a full
    blank line above it do the work the point size did."""
    return f"[{theme.text}][bold]{escape(text)}[/bold][/]"


def _stat(theme: Theme, name: str, value: str, note: str = "") -> str:
    line = f"{label(name, theme)}  [{theme.text}][bold]{escape(value)}[/bold][/]"
    if note:
        line += f"  [{theme.text_soft}]{escape(note)}[/]"
    return line


def action_bar(theme: Theme, key: str, sentence: str, trailing: str = "") -> str:
    """The filled cobalt strip. This is the single most important element in the
    design: it is the promise that Enter does exactly what it says."""
    on = theme.on_accent()
    text = f"[on {theme.accent}][{on}][bold] {key} [/bold] {escape(sentence)}"
    if trailing:
        text += f"  [{on}]{escape(trailing)}[/]"
    return text + " [/][/]"


def hint_line(theme: Theme, pairs: list[tuple[str, str]]) -> str:
    return "  ".join(
        f"[{theme.text}][bold]{k}[/bold][/] [{theme.text_soft}]{v}[/]" for k, v in pairs
    )


def detail_running(agent, session: Session | None, theme: Theme, usage_note: str = "",
                   turns=(), width: int = 76) -> str:
    """S1 · a live agent is selected. The pane answers "what is it doing" and
    Enter jumps to its tab."""
    colour = state_colour(agent.state, theme)
    facts = f"{agent.tool} · {agent.tty} · up {agent.elapsed}"
    lines = [
        _eyebrow(theme, colour, state_words(agent.state), facts),
        "",
        _heading(theme, agent.title or "(untitled session)"),
        f"[{theme.text_soft}]{escape(_tilde(agent.cwd))}[/]",
        "",
        "  ".join(
            filter(
                None,
                [
                    _stat(theme, "messages", str(session.n_messages) if session else "—"),
                    _stat(theme, "tokens", fmt_tokens(agent.tokens or (session.tokens if session else 0))),
                ],
            )
        ),
    ]
    if usage_note:
        lines.append(_stat(theme, f"{agent.tool} window", usage_note))
    if agent.label:
        lines += ["", label("doing now", theme), f"[{colour}]{escape(agent.label)}[/]"]
    if turns:
        lines += ["", transcript_block(turns, theme, width, agent.tool)]
    elif session and session.first_prompt:
        lines += ["", label("what you asked for", theme),
                  f"[{theme.text_soft}]{escape(session.first_prompt)}[/]"]
    return "\n".join(lines)


def detail_session(session: Session, theme: Theme, usage_note: str = "",
                   turns=(), width: int = 76) -> str:
    """S2 · a finished session is selected. The pane shows the literal resume
    command, because the thing you actually want from history is a way back in."""
    facts = f"{session.tool} · last active {rel_time(session.last_active)}"
    lines = [
        _eyebrow(theme, theme.text_dim, "ended", facts),
        "",
        _heading(theme, session.title or "(untitled session)"),
        f"[{theme.text_soft}]{escape(_tilde(session.project_dir))}[/]",
        "",
        "  ".join(
            [
                _stat(theme, "messages", str(session.n_messages)),
                _stat(theme, "tokens", fmt_tokens(session.tokens)),
            ]
        ),
    ]
    if usage_note:
        lines.append(_stat(theme, f"{session.tool} window", usage_note))
    if turns:
        lines += ["", transcript_block(turns, theme, width, session.tool)]
    elif session.first_prompt:
        lines += ["", label("what you asked for", theme),
                  f"[{theme.text_soft}]{escape(session.first_prompt)}[/]"]
    lines += ["", label("resume command", theme),
              f"[on {theme.raised}][{theme.text_soft}] {escape(resume_invocation(session))} [/][/]"]
    return "\n".join(lines)


def detail_empty(theme: Theme) -> str:
    """Nothing selected, which on a fresh machine is the first thing you see."""
    return "\n".join(
        [
            label("nothing selected", theme),
            "",
            f"[{theme.text_soft}]No sessions yet, and nothing running.[/]",
            f"[{theme.text_soft}]Press [bold]n[/bold] to start an agent somewhere.[/]",
        ]
    )


# -------------------------------------------------------------------- launcher

def launch_sentence(
    tools: tuple[str, ...],
    tool_idx: int,
    dirs: list[tuple[str, int, datetime | None]],
    dir_idx: int,
) -> str:
    """"open codex in ~/Repos/ai-next" — the promise the action bar makes.

    Shared by the launcher body and the pinned action bar so the two can never
    drift into describing different outcomes.
    """
    tool = tools[tool_idx]
    verb = "open" if tool != "terminal" else "open a shell in"
    target = _tilde(dirs[dir_idx][0]) if dir_idx < len(dirs) else "a path you type"
    return f"{verb} {tool} in {target}"


def launcher(
    theme: Theme,
    tools: tuple[str, ...],
    tool_idx: int,
    dirs: list[tuple[str, int, datetime | None]],
    dir_idx: int,
    opened: list[str],
    plan_label: str = "",
    plan_hint: str = "",
    include_action: bool = True,
) -> str:
    """S3 · the new-session pane.

    This is the old `#launch` chip row and `#picker` list merged into one view
    inside the detail pane, and it keeps every bit of the old behaviour: five
    tools, the cwd pinned first, recency and session counts per directory, a
    typed-path row, and Enter opening one Warp tab per press without closing,
    so several tabs is just several presses.
    """
    chips = []
    for i, tool in enumerate(tools):
        if i == tool_idx:
            chips.append(f"[on {theme.accent}][{theme.on_accent()}][bold] {tool} [/bold][/][/]")
        else:
            chips.append(f"[{theme.text_soft}] {tool} [/]")
    lines = [
        f"{label('new session', theme, strong=True)}"
        f"    [{theme.text_dim}]← → pick a tool · esc cancels[/]",
        "",
        " ".join(chips),
    ]
    if plan_label:
        line = f"[{theme.text_soft}]running as[/] [{theme.text}][bold]{escape(plan_label)}[/bold][/]"
        if plan_hint:
            line += f"  [{theme.text_dim}]{escape(plan_hint)}[/]"
        lines += ["", line]
    lines += ["", f"{label('where', theme)}    [{theme.text_dim}]↑ ↓ to move[/]"]

    for i, (path, count, last) in enumerate(dirs):
        lines.append(_dir_row(theme, path, count, last, selected=(i == dir_idx),
                              opened=path in opened, current=(i == 0)))
    lines.append(_typed_path_row(theme, selected=(dir_idx == len(dirs))))

    if include_action:
        lines += ["", action_bar(theme, "⏎", launch_sentence(tools, tool_idx, dirs, dir_idx),
                                 "stays open for the next one")]
    if opened:
        names = ", ".join(_short_project(p) for p in opened)
        lines.append(f"[{theme.text_soft}]opened this run:[/] [{theme.text}]{escape(names)}[/]")
    return "\n".join(lines)


def _dir_row(theme, path, count, last, *, selected, opened, current) -> str:
    bar = f"[{theme.accent}]{SELECTION_BAR}[/]" if selected else " "
    name = _tilde(path)
    colour = theme.text
    meta = "current" if current else (compact_stamp(rel_time(last)) if last else "")
    if count:
        meta = f"{meta} · {count} session{'s' if count != 1 else ''}" if meta else f"{count} sessions"
    mark = f"[{theme.running}]✓ opened[/]" if opened else ""
    weight = "[bold]" if selected else ""
    close = "[/bold]" if selected else ""
    return (
        f"{bar} [{colour}]{weight}{escape(truncate(name, 34))}{close}[/]"
        f"  {mark}  [{theme.text_dim}]{escape(meta)}[/]"
    )


def _typed_path_row(theme, *, selected) -> str:
    bar = f"[{theme.accent}]{SELECTION_BAR}[/]" if selected else " "
    return f"{bar} [{theme.text_soft}]+ type a path…[/]  [{theme.text_dim}]~ expands[/]"


# ---------------------------------------------------------------------- search

def search_header(theme: Theme, query: str, shown: int, total: int) -> str:
    """S4 · the header turns into the query field. The count is the honest part:
    it tells you how much of your history you are currently not looking at."""
    field = f"[on {theme.raised}][{theme.accent}] ⌕ [/][{theme.text}]{escape(query)}[/][{theme.accent}]▌ [/][/]"
    return f"{field}  [{theme.text_soft}]{shown} of {total} sessions[/]"


def match_block(theme: Theme, snippets: list[str], where: str) -> str:
    """The quoted lines a search hit on, shown under the normal detail so you
    can tell *why* a session matched without opening it."""
    if not snippets:
        return ""
    lines = [label(f"matched in {where}", theme)]
    for i, snippet in enumerate(snippets[:3]):
        accent = theme.accent if i == 0 else theme.rule
        lines.append(f"[{accent}]▎[/] [{theme.text_soft}]{escape(snippet)}[/]")
    return "\n".join(lines)


def transcript_block(turns, theme: Theme, width: int = 76, agent_name: str = "agent") -> str:
    """The conversation, rendered the way you would have watched it happen.

    Oldest at the top, newest at the bottom, speaker on its own line and the
    body indented under it. That indent is doing the real work: it is what a
    terminal transcript looks like, and it means you can skim the left edge for
    who was talking without reading a word.
    """
    if not turns:
        return ""
    lines = [label("conversation", theme)]
    for turn in turns:
        if turn.role == "user":
            who, colour, body = "you", theme.accent, theme.text
        else:
            who, colour, body = agent_name, theme.text_soft, theme.text_soft
        lines.append(f"[{colour}][bold]{who}[/bold][/]")
        for wrapped in _wrap(turn.text, max(20, width - 2)):
            lines.append(f"  [{body}]{escape(wrapped)}[/]")
        lines.append("")
    return "\n".join(lines).rstrip()


def _wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap. Rich would wrap this for us, but then the indent would
    only apply to the first line and the speaker columns would collapse."""
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


def _tilde(path: str) -> str:
    from pathlib import Path

    home = str(Path.home())
    return path.replace(home, "~", 1) if path and path.startswith(home) else (path or "?")
