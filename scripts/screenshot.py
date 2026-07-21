#!/usr/bin/env python3
"""Render the dashboard headlessly with synthetic demo data and save the README
screenshots (adash.svg + adash.png). No real sessions, paths, or usage figures
are read, so the output is always safe to publish."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_module
from agents import RunningAgent
from app import AdashApp
from models import Session
from usage import ToolUsage, UsageWindow

HOME = str(Path.home())
NOW = datetime.now(timezone.utc)


def _ago(**kwargs) -> datetime:
    return NOW - timedelta(**kwargs)


def _demo_sessions() -> list[Session]:
    return [
        Session("claude", "demo-c1", "Redesign the settings screen", f"{HOME}/web-app",
                _ago(minutes=6), n_messages=42, tokens=184_000),
        Session("opencode", "demo-o1", "Fix timezone bug in scheduler", f"{HOME}/api-server",
                _ago(minutes=23), n_messages=18, tokens=56_000),
        Session("codex", "demo-x1", "Add retry logic to webhook delivery", f"{HOME}/api-server",
                _ago(hours=2), n_messages=31, tokens=97_000),
        Session("claude", "demo-c2", "Write onboarding email copy", f"{HOME}/marketing-site",
                _ago(hours=5), n_messages=12, tokens=41_000),
        Session("codex", "demo-x2", "Migrate database connection pooling", f"{HOME}/api-server",
                _ago(hours=26), n_messages=64, tokens=233_000),
        Session("opencode", "demo-o2", "Add dark mode to docs theme", f"{HOME}/docs",
                _ago(days=2), n_messages=9, tokens=22_000),
        Session("claude", "demo-c3", "Debug flaky checkout test", f"{HOME}/web-app",
                _ago(days=3), n_messages=27, tokens=88_000),
    ]


def _demo_usage() -> tuple[list[ToolUsage], datetime]:
    return [
        ToolUsage(tool="claude", plan="Team",
                  windows=[UsageWindow("5h", 46.0), UsageWindow("7d", 21.0)]),
        ToolUsage(tool="codex", plan="Pro", windows=[UsageWindow("7d", 83.0)]),
        ToolUsage(tool="opencode", note="no subscription · API spend 7d: $4.20 across 8 sessions"),
    ], NOW


def _demo_running() -> list[RunningAgent]:
    return [
        RunningAgent(tool="claude", pid=59001, tty="ttys001", elapsed="27m",
                     cwd=f"{HOME}/web-app", title="Redesign the settings screen",
                     state="working", label="editing Settings.tsx", tokens=184_000),
        RunningAgent(tool="opencode", pid=59002, tty="ttys004", elapsed="6m",
                     cwd=f"{HOME}/api-server", title="Fix timezone bug in scheduler",
                     state="waiting", label="waiting for input", tokens=56_000),
    ]


async def main() -> None:
    app_module.collect_all = lambda limit=300: _demo_sessions()
    app_module.collect_usage = lambda active_claude=None, force=False: _demo_usage()
    app_module.running_agents = lambda: _demo_running()
    app_module.enrich = lambda agents: agents  # demo agents arrive pre-enriched

    app = AdashApp(cmd_file="/tmp/adash-shot-cmd")
    async with app.run_test(size=(130, 40)) as pilot:
        for _ in range(200):
            await pilot.pause(0.1)
            if app.sessions and app.usages and app.running:
                break
        await pilot.pause(0.3)
        out = Path(__file__).parent.parent / "adash.svg"
        app.save_screenshot(str(out))
        print(f"saved {out}")

    try:
        import cairosvg

        png = out.with_suffix(".png")
        cairosvg.svg2png(url=str(out), write_to=str(png), scale=2)
        print(f"saved {png}")
    except ImportError:
        print("cairosvg not installed; skipped adash.png (pip install '.[dev]')")


if __name__ == "__main__":
    asyncio.run(main())
