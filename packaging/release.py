"""A release: Qualm.app built, its DMG signed for Sparkle, the update feed
written and signed, the notes taken from docs/CHANGELOG.md; with --publish, all
of it on GitHub Releases. The release workflow (.github/workflows/release.yml)
runs this for a pushed tag; by hand it's the same:

    uv run python packaging/release.py              # dist/Qualm.dmg, dist/appcast.xml, dist/release-notes.md
    uv run python packaging/release.py --publish    # and the GitHub release for tag v<version> (it must be pushed)

The tag is v + pyproject.toml's version (v0.1.0b1). Signing uses the private
EdDSA key from SPARKLE_ED_PRIVATE_KEY (CI's secret), --key-file, or else the
login keychain (where Sparkle's generate_keys saved it). Every release is
published as the latest, betas too: the feed is appcast.xml on the latest
release, so a pre-release would never reach anyone's Check for Updates.
"""

from __future__ import annotations

import argparse
import email.utils
import os
import plistlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_app  # noqa: E402

from qualm.updates import REPO, SPARKLE  # noqa: E402

ROOT = build_app.REPO
DIST = build_app.DIST
CHANGELOG = ROOT / "docs" / "CHANGELOG.md"


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def pretty(v: str) -> str:
    """0.1.0b1 -> 0.1.0 beta 1, for titles."""
    m = re.fullmatch(r"(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?", v)
    if not m or not m.group(2):
        return v
    return f"{m.group(1)} {dict(a='alpha', b='beta', rc='release candidate')[m.group(2)]} {m.group(3)}"


def notes(v: str, changelog: Path = CHANGELOG) -> str:
    """The section of docs/CHANGELOG.md headed `## <version>`, without its heading."""
    text = changelog.read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(v)}\b.*?$\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m or not m.group(1).strip():
        sys.exit(f"{changelog} has no section `## {v}`: write what changed first")
    return m.group(1).strip() + "\n"


def unwrap(markdown: str) -> str:
    """One line per paragraph and per list item: the changelog wraps at 100
    columns, and Sparkle's Markdown shows a wrapped line's break."""
    out: list[str] = []
    for line in markdown.splitlines():
        s = line.strip()
        if s and out and out[-1] and not re.match(r"([-*+]|\d+\.)\s", s) and not s.startswith("#"):
            out[-1] += " " + s
        else:
            out.append(s if not s or not line.startswith(" ") else line.rstrip())
    return "\n".join(out).strip() + "\n"


def sign(path: Path, key_file: str | None) -> str:
    """sign_update on `path`: for an archive, its enclosure attributes
    (sparkle:edSignature="…" length="…"); a feed is signed in place ("")."""
    tool = build_app.sparkle() / "bin" / "sign_update"
    key = os.environ.get("SPARKLE_ED_PRIVATE_KEY")
    cmd = [str(tool)]
    if key:
        cmd += ["--ed-key-file", "-"]
    elif key_file:
        cmd += ["--ed-key-file", key_file]
    cmd += [str(path)] + (["--disable-signing-warning"] if path.suffix == ".xml" else [])
    out = subprocess.run(cmd, input=key or "", capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"sign_update failed on {path.name}: {out.stderr.strip() or out.stdout.strip()}")
    return out.stdout.strip()


def appcast(v: str, build: int, tag: str, enclosure: str, body: str, min_os: str) -> str:
    """The feed: one item, the release it's attached to. Sparkle compares
    sparkle:version (the build number); the notes are Markdown."""
    base = f"https://github.com/{REPO}/releases"
    attrs = dict(re.findall(r'([\w:]+)="([^"]*)"', enclosure))
    if "sparkle:edSignature" not in attrs or "length" not in attrs:
        sys.exit(f"sign_update gave no signature and length: {enclosure!r}")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="{SPARKLE}">
  <channel>
    <title>Qualm</title>
    <link>{base}</link>
    <description>Qualm's releases</description>
    <language>en</language>
    <item>
      <title>Qualm {escape(v)}</title>
      <pubDate>{email.utils.formatdate(usegmt=True)}</pubDate>
      <sparkle:version>{build}</sparkle:version>
      <sparkle:shortVersionString>{escape(v)}</sparkle:shortVersionString>
      <sparkle:minimumSystemVersion>{escape(min_os)}</sparkle:minimumSystemVersion>
      <sparkle:fullReleaseNotesLink>{base}/tag/{escape(tag)}</sparkle:fullReleaseNotesLink>
      <description sparkle:format="markdown"><![CDATA[{unwrap(body).replace("]]>", "]]&gt;")}]]></description>
      <enclosure url={quoteattr(f"{base}/download/{tag}/Qualm.dmg")} length="{attrs['length']}" type="application/octet-stream" sparkle:edSignature="{attrs['sparkle:edSignature']}"/>
    </item>
  </channel>
</rss>
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--skip-build", action="store_true", help="use dist/Qualm.app and dist/Qualm.dmg as they are")
    p.add_argument("--key-file", help="the private EdDSA key (default: $SPARKLE_ED_PRIVATE_KEY, else the keychain)")
    p.add_argument("--publish", action="store_true", help="create the GitHub release for the tag, with the DMG and "
                   "the feed (gh; the tag must be pushed)")
    args = p.parse_args()

    v = version()
    tag = f"v{v}"
    if (ref := os.environ.get("GITHUB_REF_NAME")) and ref != tag:
        sys.exit(f"the tag {ref} isn't v + pyproject.toml's version ({tag}): bump the version or fix the tag")
    body = notes(v)  # before a 3-minute build: no notes, no release
    if not args.skip_build:
        build_app.main()
    app, dmg = DIST / "Qualm.app", DIST / "Qualm.dmg"
    with (app / "Contents" / "Info.plist").open("rb") as f:
        info = plistlib.load(f)
    if info["CFBundleShortVersionString"] != v:
        sys.exit(f"{app} is version {info['CFBundleShortVersionString']}, not {v}: build again")
    build = int(info["CFBundleVersion"])

    feed = DIST / "appcast.xml"
    feed.write_text(appcast(v, build, tag, sign(dmg, args.key_file), body, info.get("LSMinimumSystemVersion", "13.0")),
                    encoding="utf-8")
    sign(feed, args.key_file)
    (DIST / "release-notes.md").write_text(body, encoding="utf-8")
    print(f"\nQualm {v} (build {build}), tag {tag}:\n  {dmg}\n  {feed} (signed)\n  {DIST / 'release-notes.md'}")

    if args.publish:
        subprocess.run(["gh", "release", "create", tag, str(dmg), str(feed), "--repo", REPO, "--verify-tag",
                        "--latest", "--title", f"Qualm {pretty(v)}", "--notes-file", str(DIST / "release-notes.md")],
                       check=True)
        print(f"published: https://github.com/{REPO}/releases/tag/{tag}")


if __name__ == "__main__":
    main()
