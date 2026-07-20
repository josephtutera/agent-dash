"""Detect currently running claude / codex / opencode terminal sessions.

Scans the process table for the three CLI binaries with a real controlling
terminal (which filters out desktop apps, dev servers, and crashpad helpers),
then resolves each process's working directory so the dashboard can match it
back to a session title.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

_TOOL_PATTERNS = {
    "claude": re.compile(r"(?:^|\s|/)claude(?:\s|$)"),
    "codex": re.compile(r"(?:^|\s|/)codex(?:\s|$)"),
    "opencode": re.compile(r"(?:^|\s|/)opencode(?:\s|$)"),
}

# lines that look like a match but aren't an interactive agent session
_EXCLUDE = re.compile(r"adash|grep|pgrep|/bin/z?sh -c|/bin/ba?sh -c|-z?sh\b|--version|--help")


@dataclass
class RunningAgent:
    tool: str
    pid: int
    tty: str
    elapsed: str  # friendly: "4h 12m"
    cwd: str
    title: str = ""  # resolved later from the session list
    # live activity, filled in by activity.enrich (defaults = not-yet-known)
    state: str = "unknown"  # "working" | "idle" | "waiting" | "unknown"
    label: str = ""  # current action, e.g. "running command"
    tokens: int = 0  # live token count for this session (0 if unknown)


def _elapsed(etime: str) -> str:
    """ps etime is [[dd-]hh:]mm:ss; compress to '2d 4h' / '4h 12m' / '12m'."""
    days = 0
    if "-" in etime:
        day_part, etime = etime.split("-", 1)
        days = int(day_part)
    parts = [int(p) for p in etime.split(":")]
    if len(parts) == 3:
        hours, minutes = parts[0], parts[1]
    elif len(parts) == 2:
        hours, minutes = 0, parts[0]
    else:
        hours, minutes = 0, 0
    total_min = days * 24 * 60 + hours * 60 + minutes
    if total_min >= 24 * 60:
        return f"{total_min // (24 * 60)}d {(total_min % (24 * 60)) // 60}h"
    if total_min >= 60:
        return f"{total_min // 60}h {total_min % 60}m"
    return f"{max(total_min, 1)}m"


def _parse_ps(output: str) -> list[RunningAgent]:
    agents = []
    for line in output.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, tty, etime, cmdline = parts
        if tty == "??" or _EXCLUDE.search(cmdline):
            continue
        for tool, pattern in _TOOL_PATTERNS.items():
            if pattern.search(cmdline):
                agents.append(
                    RunningAgent(tool=tool, pid=int(pid), tty=tty, elapsed=_elapsed(etime), cwd="")
                )
                break
    return agents


def _cwd_for_pid(pid: int) -> str:
    try:
        out = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in out.stdout.splitlines():
        if line.startswith("n/"):
            return line[1:]
    return ""


def running_agents() -> list[RunningAgent]:
    try:
        out = subprocess.run(
            ["ps", "ax", "-o", "pid=,tty=,etime=,command=", "-ww"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    agents = _parse_ps(out.stdout)
    for agent in agents:
        agent.cwd = _cwd_for_pid(agent.pid)
    agents.sort(key=lambda a: (a.tool, a.pid))
    return agents
