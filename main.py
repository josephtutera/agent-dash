#!/usr/bin/env python3
"""adash: one dashboard for your Claude Code, Codex, and OpenCode sessions."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from collectors import collect_all
from models import fmt_tokens, rel_time, resume_command

# shown as the Warp tab title while the TUI runs, instead of the raw "adash" command
TAB_TITLE = "◆ Agent Dash"


def set_tab_title(title: str) -> None:
    """Set the terminal tab title via OSC 0/1/2.

    Warp (and iTerm2, Terminal.app) show this in place of the running command
    name. An empty string clears the override so the tab falls back to the
    default process/cwd display.
    """
    if os.environ.get("TERM") == "dumb":
        return
    for osc in (0, 1, 2):
        sys.stdout.write(f"\x1b]{osc};{title}\x07")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(prog="adash")
    parser.add_argument("command", nargs="?", choices=["serve"],
                        help="run a subcommand (serve = resident HUD snapshot daemon)")
    parser.add_argument("--cmd-file", help="write the chosen resume command to this file")
    parser.add_argument("--limit", type=int, default=300, help="max sessions to scan per tool")
    parser.add_argument("--dump", action="store_true", help="print sessions as text and exit (no TUI)")
    parser.add_argument("--usage", action="store_true", help="print subscription usage and exit (no TUI)")
    parser.add_argument("--serve", action="store_true", help="run the resident HUD snapshot daemon")
    parser.add_argument("--host", default="127.0.0.1",
                        help="serve: host to bind (default 127.0.0.1; non-loopback needs ADASH_SERVE_ALLOW_REMOTE=1)")
    parser.add_argument("--port", type=int, default=8737, help="serve: port to bind (default 8737)")
    args = parser.parse_args()

    if args.command == "serve" or args.serve:
        from serve import serve

        serve(host=args.host, port=args.port)
        return

    if args.usage:
        from usage import collect_usage

        usages, fetched_at = collect_usage()
        for u in usages:
            windows = "  ".join(f"{w.label}={w.pct}% (resets {w.resets_at})" for w in u.windows)
            print(f"{u.tool:9} {u.plan:12} {windows} {u.note} {u.error or ''}")
        return

    if args.dump:
        for s in collect_all(limit=args.limit):
            print(f"{s.tool:9} {rel_time(s.last_active):>9} {fmt_tokens(s.tokens):>7}  {s.title[:70]}")
        return

    from app import AdashApp

    set_tab_title(TAB_TITLE)
    try:
        app = AdashApp(cmd_file=args.cmd_file, limit=args.limit)
        app.run()
    finally:
        set_tab_title("")  # hand the tab back to the shell's process/cwd display
    if app.selected and not args.cmd_file:
        # launched without the shell wrapper: print the command to copy-paste
        print(resume_command(app.selected))


if __name__ == "__main__":
    main()
