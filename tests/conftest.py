"""Shared test hardening: keep the suite deterministic regardless of where and
under what wrapper it runs.

- The picker logic treats the current directory as meaningful (it pins cwd,
  and it excludes agent worktrees). Developers and CI run pytest from anywhere,
  including inside a `.claude/worktrees/` checkout, so every test runs chdir'd
  into its own temp directory to make cwd-derived behavior reproducible.
- The serve tests hit `http://127.0.0.1` with urllib, which honors HTTP_PROXY
  from the environment. Proxy wrappers (Socket Firewall, corporate proxies)
  would intercept those loopback requests and fail them, so proxy variables
  are stripped for every test.
"""

import pytest


@pytest.fixture(autouse=True)
def _neutral_cwd(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _no_proxies(monkeypatch: pytest.MonkeyPatch):
    for var in (
        "HTTP_PROXY", "http_proxy",
        "HTTPS_PROXY", "https_proxy",
        "ALL_PROXY", "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
