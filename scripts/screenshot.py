#!/usr/bin/env python3
"""Render the real dashboard headlessly and save an SVG screenshot."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import AdashApp
from textual.widgets import DataTable


async def main() -> None:
    app = AdashApp(cmd_file="/tmp/adash-shot-cmd")
    async with app.run_test(size=(130, 40)) as pilot:
        table = app.query_one("#sessions", DataTable)
        for _ in range(200):
            await pilot.pause(0.1)
            if table.row_count and app.usages and app.running:
                break
        await pilot.pause(0.3)
        out = Path(__file__).parent.parent / "adash.svg"
        app.save_screenshot(str(out))
        print(f"saved {out} ({table.row_count} rows)")


if __name__ == "__main__":
    asyncio.run(main())
