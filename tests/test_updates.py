"""Updates: versions compared as Sparkle and PEP 440 do, the feed a release writes read back, the
checkout's daily check and what it says, the build's Info.plist, and the release's notes. No
network: feeds are files (file:// URLs), and nothing is ever sent to GitHub."""

import json
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from qualm import updates

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packaging"))
import build_app  # noqa: E402
import release  # noqa: E402

ENCLOSURE = 'sparkle:edSignature="c2lnbmF0dXJl" length="94023235"'


def feed(tmp_path, version="0.1.1", build=60, notes="- **New.** Something.\n"):
    """A feed as the release writes it, signed-feed comment and all, as a file:// URL."""
    xml = release.appcast(version, build, f"v{version}", ENCLOSURE, notes, "13.0")
    xml += "<!-- sparkle-signatures:\nedSignature: abc\nlength: 1\n-->\n"
    path = tmp_path / "appcast.xml"
    path.write_text(xml, encoding="utf-8")
    return path.as_uri()


def test_versions_compare_as_pep_440_does():
    order = ["0.1.0a1", "0.1.0b1", "0.1.0b2", "0.1.0rc1", "0.1.0", "0.1.1", "0.2", "1.0"]
    keys = [updates.vkey(v) for v in order]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    assert updates.vkey("0.1") == updates.vkey("0.1.0") == updates.vkey("v0.1.0")
    assert updates.vkey("0.1.0-beta.1") == updates.vkey("0.1.0b1")
    assert updates.vkey("nightly") is None


def test_newer_goes_by_build_number_when_both_have_one():
    r = updates.Release("0.1.0b1", 51, "page", "dmg")
    assert updates.newer(r, "0.1.0b1", 50)  # same version, a later build: Sparkle offers it too
    assert not updates.newer(r, "0.2.0", 51)
    assert updates.newer(r, "0.1.0a3") and not updates.newer(r, "0.1.0b1") and not updates.newer(r, "0.1.0")
    assert not updates.newer(updates.Release("nightly", None, "", ""), "0.1.0")  # unreadable: never newer


def test_the_feed_a_release_writes_reads_back(tmp_path):
    [r] = updates.fetch(feed(tmp_path, notes="Line one\n  continued.\n\n- a\n  b\n"))
    assert (r.version, r.build) == ("0.1.1", 60)
    assert r.page == "https://github.com/RoderickQiu/qualm/releases/tag/v0.1.1"
    assert r.download == "https://github.com/RoderickQiu/qualm/releases/download/v0.1.1/Qualm.dmg"
    assert r.notes == "Line one continued.\n\n- a b"  # unwrapped for Sparkle's Markdown
    xml = (tmp_path / "appcast.xml").read_text()
    assert 'sparkle:edSignature="c2lnbmF0dXJl"' in xml and 'length="94023235"' in xml
    assert 'sparkle:format="markdown"' in xml


def test_a_check_says_whether_theres_something_newer(tmp_path, monkeypatch):
    url = feed(tmp_path, "0.1.1", 60)
    monkeypatch.setattr(updates, "version", lambda: "0.1.0b1")
    monkeypatch.setattr(updates, "build", lambda: None)
    got = updates.check(url)
    assert got["update"] and got["latest"]["version"] == "0.1.1" and got["feed"] == url
    monkeypatch.setattr(updates, "version", lambda: "0.1.1")
    assert not updates.check(url)["update"]
    (tmp_path / "bad.xml").write_text("<html>not a feed")
    with pytest.raises(ValueError):
        updates.fetch((tmp_path / "bad.xml").as_uri())
    with pytest.raises(OSError):
        updates.fetch((tmp_path / "missing.xml").as_uri())


def test_the_checkouts_daily_check_and_its_state(tmp_path):
    assert updates.due({}) and updates.load_state(tmp_path) == {}
    now = time.time()
    assert not updates.due({"checked": now - 3600}, now)
    assert updates.due({"checked": now - updates.CHECK_EVERY_S - 1}, now)
    assert not updates.due({"auto": False}, now)
    updates.save_state(tmp_path, auto=False, ran="0.1.0b1")
    assert updates.load_state(tmp_path) == {"auto": False, "ran": "0.1.0b1"}
    (tmp_path / updates.STATE).write_text("{torn")
    assert updates.load_state(tmp_path) == {}
    assert updates.updated_since({}, "51") is None
    assert updates.updated_since({"ran": "51"}, "51") is None
    assert updates.updated_since({"ran": 50}, "51") == "50"


def test_version_command(tmp_path, monkeypatch):
    env = {**__import__("os").environ, "QUALM_HOME": str(tmp_path / "home"),
           "QUALM_UPDATE_FEED": feed(tmp_path, "99.0", 999)}

    def qualm(*args):
        return subprocess.run([sys.executable, "-m", "qualm.cli", "version", *args], capture_output=True, text=True,
                              cwd=tmp_path, env=env)

    out = qualm("--json")
    assert out.returncode == 0 and json.loads(out.stdout)["version"] == updates.version()
    out = json.loads(qualm("--check", "--json").stdout)
    assert out["update"] and out["latest"]["version"] == "99.0" and out["running_from"] == "source"
    text = qualm("--check").stdout
    assert "Qualm 99.0 is out" in text and "git pull" in text
    env["QUALM_UPDATE_FEED"] = (tmp_path / "missing.xml").as_uri()
    out = qualm("--check", "--json")
    assert out.returncode == 5 and json.loads(out.stdout)["error"]["code"] == "unreachable"


def test_the_app_is_built_to_check_daily_and_install_only_when_asked():
    info = build_app.info_plist("0.1.0b1", 51)
    assert info["CFBundleShortVersionString"] == "0.1.0b1" and info["CFBundleVersion"] == "51"
    assert info["SUFeedURL"] == updates.FEED and info["SUPublicEDKey"].endswith("=")
    assert info["SUEnableAutomaticChecks"] and info["SUScheduledCheckInterval"] == 86400
    assert info["SUAllowsAutomaticUpdates"] is False  # Accessibility must be given again: never silently
    assert info["SURequireSignedFeed"] and info["SUVerifyUpdateBeforeExtraction"]
    plistlib.dumps(info)  # every value a plist can hold


def test_release_notes_come_from_the_changelog(tmp_path):
    log = tmp_path / "CHANGELOG.md"
    log.write_text("# Changes\n\n## 0.2.0\n\nNew things,\nwrapped.\n\n## 0.1.0b1\n\nFirst.\n")
    assert release.notes("0.2.0", log) == "New things,\nwrapped.\n"
    assert release.notes("0.1.0b1", log) == "First.\n"
    with pytest.raises(SystemExit):
        release.notes("0.3.0", log)
    assert release.unwrap("a\nb\n\n- one\n  two\n- three\n") == "a b\n\n- one two\n- three\n"
    assert release.pretty("0.1.0b1") == "0.1.0 beta 1" and release.pretty("1.0.0") == "1.0.0"
    assert release.notes(release.version())  # the version being released has its notes
