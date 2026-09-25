"""What another person's AI agent finds on their Mac: Qualm's skill in Claude Code's folder
(written, kept current, never someone else's overwritten) and Qualm's logs (`qualm logs`).
Claude Code's folder is a throwaway one (conftest.py); nothing reaches the screen."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from qualm import agent, autostart, cli, paths, personalize, updates

REPO_SKILL = Path(__file__).parent.parent / ".claude" / "skills" / "qualm" / "SKILL.md"


@pytest.fixture
def main(tmp_path, monkeypatch):
    """The main install, as Qualm.app with its `qualm` command: a pretend one under tmp_path."""
    monkeypatch.setenv("QUALM_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(paths, "custom_home", lambda: False)
    monkeypatch.setattr(paths, "bundle", lambda: Path("/Applications/Qualm.app"))
    monkeypatch.setattr(paths, "LOGS", tmp_path / "Logs" / "Qualm")
    shim = tmp_path / "bin" / "qualm"
    shim.parent.mkdir()
    shim.write_text('#!/bin/sh\nexec "/Applications/Qualm.app/Contents/MacOS/Qualm" -m qualm "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setattr(autostart, "SHIM", shim)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(argv, capsys):
    try:
        cli.main(argv)
        code = 0
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out if "--json" in argv else out.out + out.err


def test_the_repos_skill_and_the_installed_one_share_description_and_guide():
    head, body = REPO_SKILL.read_text(encoding="utf-8").split("\n---\n", 1)
    assert head == f"---\nname: qualm\ndescription: {agent.SKILL_DESCRIPTION}"
    assert body.strip() == personalize.guide().strip()
    assert ": " not in agent.SKILL_DESCRIPTION and " #" not in agent.SKILL_DESCRIPTION  # a plain YAML scalar
    installed = agent.skill_text()
    assert installed.startswith(head + "\n---\n") and installed.endswith(personalize.guide())


def test_the_skill_says_how_to_run_qualm_here_by_its_full_path(main, monkeypatch):
    monkeypatch.setenv("PATH", f"{main / 'bin'}:{os.environ['PATH']}")
    assert agent.command() == "qualm"  # a terminal with it on PATH
    text = agent.skill_text()
    # Claude Code's shell may not have it on PATH: the full path
    assert f"Here the command is `{main / 'bin' / 'qualm'}`: write it wherever the guide below says `qualm`." in text
    assert f"{agent.SKILL_MARK} {updates.version()}" in text and f"`{main / 'bin' / 'qualm'} skill remove`" in text


def test_the_skill_is_written_kept_current_and_removed(main, claude_config_dir):
    path = claude_config_dir / "skills" / "qualm" / "SKILL.md"
    assert agent.claude_code() and agent.skill_state() == "none"
    assert not agent.refresh_skill() and not path.exists()  # the app only brings one it finds up to date
    assert agent.install_skill() == path and agent.skill_state() == "current"
    path.write_text(path.read_text().replace(updates.version(), "0.0.9"))  # an older Qualm wrote it
    assert agent.skill_state() == "old" and agent.refresh_skill() and path.read_text() == agent.skill_text()
    (path.parent / "notes.md").write_text("mine")
    assert agent.remove_skill() and not path.exists() and (path.parent / "notes.md").exists()  # yours stays
    (path.parent / "notes.md").unlink()
    agent.install_skill()
    assert agent.remove_skill() and not path.parent.exists()
    assert not agent.remove_skill()  # nothing of Qualm's left


def test_a_skill_qualm_didnt_write_is_never_touched(main, claude_config_dir, capsys):
    path = claude_config_dir / "skills" / "qualm" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: qualm\ndescription: my own notes on Qualm\n---\n\nWritten by me.\n")
    before = path.read_text()
    assert agent.skill_state() == "other"
    assert agent.install_skill() is None and not agent.remove_skill() and not agent.refresh_skill()
    code, out = run(["skill", "install", "--json"], capsys)
    assert code == 2 and "didn't write: left alone" in json.loads(out)["error"]["message"]
    code, out = run(["skill", "--json"], capsys)
    assert code == 0 and json.loads(out)["someone_elses"] and path.read_text() == before


def test_another_qualm_folder_leaves_claude_codes_skill_to_the_main_install(main, monkeypatch, capsys):
    monkeypatch.setattr(paths, "custom_home", lambda: True)
    assert agent.install_skill() is None and not agent.skill_file().exists()
    code, out = run(["skill", "install", "--json"], capsys)
    assert code == 2 and "QUALM_HOME is set" in json.loads(out)["error"]["message"]


def test_qualm_skill_from_the_terminal(main, claude_config_dir, capsys):
    code, out = run(["skill", "--json"], capsys)
    assert code == 0 and json.loads(out) == {"path": str(agent.skill_file()), "installed": False, "current": False,
                                             "someone_elses": False, "claude_code": True}
    code, out = run(["skill", "install"], capsys)
    assert code == 0 and "Claude Code knows Qualm now" in out and agent.skill_state() == "current"
    code, out = run(["skill", "remove", "--json"], capsys)
    assert code == 0 and not json.loads(out)["installed"] and not agent.skill_file().exists()


def test_uninstall_all_takes_the_skill_too(main, monkeypatch, capsys):
    monkeypatch.setattr(autostart, "AGENTS", main / "LaunchAgents")
    monkeypatch.setattr(autostart, "HF_HUB", main / "hub")
    monkeypatch.setattr("qualm.keychain.stored", lambda: None)
    monkeypatch.setattr("qualm.review.app_running", lambda data_dir, settings=None: None)
    agent.install_skill()
    assert (autostart.SKILL, agent.skill_file()) in autostart.everything()
    assert any("Qualm's skill for Claude Code" in line for line in autostart.uninstall_all())
    assert not agent.skill_file().parent.exists()


def logs(main, *names):
    folder = main / "Logs" / "Qualm"
    folder.mkdir(parents=True, exist_ok=True)
    for i, (name, text) in enumerate(names):
        (folder / name).write_text(text)
        os.utime(folder / name, (time.time() - 100 + i, time.time() - 100 + i))
    return folder


def test_qualm_logs_lists_them_and_shows_the_apps_last_lines(main, capsys):
    code, out = run(["logs", "--json"], capsys)
    assert code == 3 and "Qualm.app writes it when it runs" in json.loads(out)["error"]["message"]
    folder = logs(main, ("kev.log", "loading\nqualm: error offline\n"),
                  ("com.qualm.app.log", "".join(f"line {i}\n" for i in range(100))))
    code, out = run(["logs", "--json", "--lines", "3"], capsys)
    d = json.loads(out)
    assert code == 0 and d["folder"] == str(folder) and d["shown"] == str(folder / "com.qualm.app.log")
    assert d["lines"] == ["line 97", "line 98", "line 99"]
    assert [f["name"] for f in d["files"]] == ["com.qualm.app.log", "kev.log"]  # newest first
    code, out = run(["logs", "--model", "--json"], capsys)
    assert json.loads(out)["lines"] == ["loading", "qualm: error offline"]
    code, out = run(["logs", "--lines", "0"], capsys)
    assert code == 0 and "com.qualm.app.log" in out and "line 99" not in out


def test_it_shows_the_app_log_written_last(main, capsys):
    # Qualm.app once, then a checkout's `qualm app >> app.log`: the one running now wrote last.
    logs(main, ("com.qualm.app.log", "an old run\n"), ("app.log", "watching\n"), ("kev.log", "up\n"))
    code, out = run(["logs", "--json"], capsys)
    assert json.loads(out)["shown"].endswith("/app.log") and json.loads(out)["lines"] == ["watching"]


def test_status_says_where_everything_is(main, capsys, monkeypatch):
    monkeypatch.setenv("KEV_URL", "http://127.0.0.1:9")
    code, out = run(["status", "--json"], capsys)
    d = json.loads(out)["paths"]
    assert d == {"rules": str(paths.rules_file().resolve()), "data": str(paths.data_dir().resolve()),
                 "logs": str(main / "Logs" / "Qualm")}


def test_the_guide_tells_an_agent_where_the_logs_are():
    guide = personalize.guide()
    assert "`~/Library/Logs/Qualm`" in guide and "`qualm logs --json`" in guide
    assert "## When Qualm itself misbehaves" in guide


def test_qualm_app_opened_from_finder_writes_its_log(tmp_path):
    """macOS gives an app opened from Finder /dev/null for its output: it goes to the login item's log instead."""
    log = tmp_path / "Logs" / "com.qualm.app.log"
    code = ("import sys; from pathlib import Path; from qualm.watcher import log_to_file; "
            f"moved = log_to_file(Path({str(log)!r})); print('watching', moved); "
            "print('an error', file=sys.stderr)")
    subprocess.run([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    assert log.read_text().splitlines() == ["watching True", "an error"]
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout == "watching False\n" and log.read_text().count("watching") == 1  # a pipe: left alone
