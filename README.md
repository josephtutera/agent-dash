# agent-dash

One terminal dashboard for your Claude Code, Codex, OpenCode, and Gemini
sessions: agents running right now, your full searchable history, and a
launcher, in one list with one cursor.

<img width="3740" height="2438" alt="CleanShot 2026-07-20 at 13 27 43@2x" src="https://github.com/user-attachments/assets/a4735c00-ee6d-4f63-bcd7-ff029cd9235e" />

## Features

- **One list, one cursor.** Agents running right now sit at the top, your
  history sits under them, and the arrow keys walk straight through both. A
  session that is currently running appears once, as the live row.
- **The right pane says what Enter will do.** Whatever is selected, the detail
  pane ends with the sentence Enter is about to carry out: jump to that agent's
  Warp tab, or resume that session in a new one. It also shows the live tool
  steps for a running agent, and the literal `claude --resume …` command for a
  finished one (`y` copies it).
- **Colour means state, not tool.** Green is running fine, amber is waiting on
  you, and nothing else is coloured, so the agent that needs you is the only
  thing on screen that pulls. The tool is a word in the meta line.
- **Launch new sessions.** Press `n` and the detail pane becomes a launcher:
  pick claude, codex, opencode, gemini or a plain shell, pick a directory from
  your recent projects (or type one), and Enter opens a Warp tab. It stays
  open, so several tabs is several presses.
- **Light and dark.** `T` switches and remembers. `--theme light` picks one at
  launch.
- **Four keys on screen.** Everything else is behind `?`. Under 100 columns the
  detail pane steps aside and the list takes the full width.

## Requirements

- **macOS.** Claude's OAuth token is read from the macOS Keychain, and
  sessions are launched with `open`.
- **Python 3.10+**
- **[Warp](https://warp.dev)** for the new-session launcher (it drives
  `warp://tab_config/...`). The rest of the dashboard works in any terminal.
- The CLIs you want to track: `claude`, `codex`, `opencode`, and/or `gemini`,
  with some existing sessions for the dashboard to show.

## Install

```sh
git clone https://github.com/josephtutera/agent-dash.git
cd agent-dash
pip install .
adash
```

For development, use a virtualenv and the editable install:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## Usage

```sh
adash                # launch the dashboard
adash --theme light  # start in light mode (T toggles and remembers)
adash --dump         # print sessions as text, no TUI
adash --usage        # print subscription usage as text, no TUI
adash --limit 500    # max sessions to scan per tool (default 300)
```

`adash --cmd-file PATH` is meant for a shell wrapper: when you pick a session,
the resume command is written to PATH instead of printed, so your shell can
run it in the current terminal. For example, in `.zshrc`:

```sh
adash() {
  local f="$(mktemp)"
  command adash --cmd-file "$f"
  [[ -s "$f" ]] && eval "$(cat "$f")"
  rm -f "$f"
}
```

### Keys

| Key | Action |
| --- | --- |
| `↑` `↓` | move through the list |
| `↵` | do what the detail pane says |
| `n` | new session |
| `/` | search titles, prompts, projects (`esc` to close) |
| `?` | every key, in one panel |
| `1`–`5` | filter: all / claude / codex / opencode / gemini |
| `c` `x` `o` `g` `t` | launcher, pre-picked to that tool (`t` is a plain shell) |
| `tab` | switch the active Claude plan (multi-account, in the launcher) |
| `y` | copy the selected session's resume command |
| `u` | subscription usage |
| `T` | light / dark |
| `C` / `U` | clear history / undo |
| `r` / `q` | refresh / quit |

In the launcher, `←` `→` pick the tool and `↑` `↓` pick the directory; `enter`
opens a tab and leaves the launcher up. In the list, a click selects a row and
a second click on the same row commits it. The first time you jump to a tab,
macOS will ask you to grant Warp Accessibility access so the dashboard can
switch tabs for you; without it, Warp still comes to the front but you'll pick
the tab yourself.

## Where the data comes from

Everything the dashboard shows is read from data already on your machine:

- **Sessions** are parsed from the files each CLI keeps locally:
  `~/.claude/projects` (Claude Code), `~/.codex` (rollouts and history),
  OpenCode's sqlite database, and `~/.gemini/tmp/*/chats` (Gemini), with
  `~/.gemini/projects.json` mapping each session back to its project directory.
- **Claude usage** comes from the same OAuth usage endpoint the `/usage`
  command calls, using the OAuth token Claude Code stores in the macOS
  Keychain. **Codex usage** comes from rate-limit snapshots embedded in local
  rollout files, and **OpenCode spend** is summed from its local database, so
  neither of those needs a network call.

Nothing is sent anywhere except that single usage request to Anthropic, made
with your own token. There are no third-party analytics, and nothing is
uploaded.

## Development

```sh
pytest                       # run the test suite
python scripts/screenshot.py # regenerate adash.svg + adash.png with demo data
```

The screenshot script renders the app headlessly against synthetic sessions
and usage, so the images in this README never contain real session data.

## License

[MIT](LICENSE)
