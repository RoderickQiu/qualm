"""Every test: Claude Code's folder is a throwaway one. Setup and the app write
Qualm's skill there (agent.py), and fixtures that pretend to be the main
install would otherwise reach the real ~/.claude."""

import pytest


@pytest.fixture(autouse=True)
def claude_config_dir(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("claude")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(path))
    return path
