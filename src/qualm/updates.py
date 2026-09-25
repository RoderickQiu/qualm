"""Is there a newer Qualm? One feed for everyone: appcast.xml on the latest
GitHub release (packaging/release.py writes it, the release workflow uploads it).

- Qualm.app hands the feed to Sparkle, the updater most Mac apps outside the
  App Store use. It checks once a day, and says so in the menu and in one corner
  notice (Sparkle's "gentle reminders", for apps that live in the menu bar);
  it installs and relaunches only when you say so, never silently: until the
  app has a Developer ID, macOS asks for Accessibility again after each update.
  The feed and every update must carry an EdDSA signature made with the key
  whose public half is in the app (Info.plist SUPublicEDKey).
- A checkout, or `qualm version --check`, reads the same feed here, says when
  a newer version is out and links to it. Updating a checkout stays git's job.

What leaves the Mac: one request a day for the feed, to GitHub; nothing about
you or your screen. "Check for updates automatically" in the menu turns it off.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = "RoderickQiu/qualm"
RELEASES = f"https://github.com/{REPO}/releases"
FEED = f"{RELEASES}/latest/download/appcast.xml"
SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
CHECK_EVERY_S = 24 * 3600  # as Sparkle's SUScheduledCheckInterval
STATE = "updates.json"  # in data/: the checkout's automatic checks, what was told, the version that ran last
PEP440 = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-_.]?(a|b|rc|alpha|beta|c)[-_.]?(\d*))?$", re.I)
PRE = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2}


def feed_url() -> str:
    """The feed; QUALM_UPDATE_FEED points at another one (a test's)."""
    return os.environ.get("QUALM_UPDATE_FEED") or FEED


def version() -> str:
    """This copy's version, as pyproject.toml gives it (PEP 440: 0.1.0b1)."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed

    try:
        return installed("qualm-desktop")
    except PackageNotFoundError:
        return "0"


def pretty(v: str) -> str:
    """0.1.0b1 -> 0.1.0 beta 1: the version as the release and the About window name it."""
    m = re.fullmatch(r"(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?", v)
    if not m or not m.group(2):
        return v
    return f"{m.group(1)} {dict(a='alpha', b='beta', rc='release candidate')[m.group(2)]} {m.group(3)}"


def build() -> int | None:
    """Qualm.app's build number (CFBundleVersion: the commit count it was built
    from), which Sparkle compares; None outside the app."""
    from . import paths

    if (app := paths.bundle()) is None:
        return None
    try:
        with (app / "Contents" / "Info.plist").open("rb") as f:
            return int(plistlib.load(f)["CFBundleVersion"])
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
        return None


def vkey(v: str) -> tuple | None:
    """A version as something to compare: 0.1.0b1 < 0.1.0rc1 < 0.1.0 < 0.1.1.
    None for a version this doesn't read (it's then never called newer)."""
    m = PEP440.match(v.strip())
    if not m:
        return None
    nums = [int(n) for n in m.group(1).split(".")]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    pre = (PRE[m.group(2).lower()], int(m.group(3) or 0)) if m.group(2) else (9, 0)
    return tuple(nums), pre


@dataclass
class Release:
    version: str  # sparkle:shortVersionString, as pyproject.toml has it
    build: int | None  # sparkle:version
    page: str  # the release on GitHub: what's new
    download: str  # the DMG
    notes: str = ""
    published: str = ""


def parse_feed(xml: bytes | str) -> list[Release]:
    """The releases a Sparkle appcast lists (ours: the newest one)."""
    root = ET.fromstring(xml)
    out = []
    for item in root.iter("item"):
        enclosure = item.find("enclosure")
        short = item.findtext(f"{{{SPARKLE}}}shortVersionString") or (
            enclosure.get(f"{{{SPARKLE}}}shortVersionString", "") if enclosure is not None else "")
        number = item.findtext(f"{{{SPARKLE}}}version") or (
            enclosure.get(f"{{{SPARKLE}}}version", "") if enclosure is not None else "")
        if not short and not number:
            continue
        out.append(Release(
            version=(short or number).strip(),
            build=int(number) if number.strip().isdigit() else None,
            page=(item.findtext(f"{{{SPARKLE}}}fullReleaseNotesLink") or item.findtext("link") or RELEASES).strip(),
            download=enclosure.get("url", "") if enclosure is not None else "",
            notes=(item.findtext("description") or "").strip(),
            published=(item.findtext("pubDate") or "").strip()))
    return out


def newer(release: Release, current: str, current_build: int | None = None) -> bool:
    """Is `release` newer than what runs here? By build number when both have
    one (as Sparkle decides), else by version."""
    if release.build is not None and current_build is not None:
        return release.build > current_build
    a, b = vkey(release.version), vkey(current)
    return a is not None and b is not None and a > b


def newest(releases: list[Release]) -> Release | None:
    def key(r: Release):
        return (r.build or 0, vkey(r.version) or ((), (0, 0)))

    return max(releases, key=key, default=None)


def fetch(url: str | None = None, timeout: float = 10.0) -> list[Release]:
    """The feed's releases. Raises OSError (the network, a 404) or ValueError
    (not a feed)."""
    req = urllib.request.Request(url or feed_url(), headers={"User-Agent": f"Qualm/{version()}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    try:
        return parse_feed(body)
    except ET.ParseError as e:
        raise ValueError(f"the update feed isn't an appcast: {e}") from None


def check(url: str | None = None, timeout: float = 10.0) -> dict:
    """One check, for the CLI and the checkout's menu: what runs here, the
    newest release, and whether it's newer. Raises as fetch() does."""
    current, number = version(), build()
    latest = newest(fetch(url, timeout))
    return {"version": current, "build": number, "latest": asdict(latest) if latest else None,
            "update": bool(latest and newer(latest, current, number)), "feed": url or feed_url()}


# -- the checkout's own schedule (Qualm.app's is Sparkle's) --------------------


def load_state(data_dir: Path) -> dict:
    """data/updates.json: {"auto": bool, "checked": epoch, "latest": {...},
    "told": version, "ran": version or build}; {} when there's none (or it's torn)."""
    try:
        state = json.loads((Path(data_dir) / STATE).read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data_dir: Path, **changes) -> dict:
    state = load_state(data_dir) | changes
    path = Path(data_dir) / STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{STATE}.{os.getpid()}")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return state


def due(state: dict, now: float | None = None) -> bool:
    """A checkout's daily check: on unless turned off, once a day."""
    if state.get("auto", True) is False:
        return False
    return (now or time.time()) - float(state.get("checked") or 0) >= CHECK_EVERY_S


def updated_since(state: dict, current: str) -> str | None:
    """The version that ran before this one, when it isn't this one: an update
    (or a downgrade) happened since. None the first time and after none."""
    before = state.get("ran")
    return str(before) if before and str(before) != current else None


# -- Sparkle, inside Qualm.app ---------------------------------------------------


def sparkle_framework() -> Path | None:
    from . import paths

    app = paths.bundle()
    fw = app / "Contents" / "Frameworks" / "Sparkle.framework" if app else None
    return fw if fw and fw.exists() else None


_DELEGATE = None
_KEEP: list = []  # Sparkle holds its delegates weakly


def start_sparkle(available, attended):
    """Sparkle's updater, started, when this is Qualm.app with Sparkle inside;
    None otherwise (a checkout, an older build). `available(version)` is called
    on the main thread when a scheduled check finds an update and leaves the
    telling to us (the menu line, a notice); `attended()` when you've seen it
    (Sparkle's window came up, or the session ended)."""
    global _DELEGATE

    fw = sparkle_framework()
    if fw is None:
        return None
    import objc
    from Foundation import NSBundle, NSObject

    bundle = NSBundle.bundleWithPath_(str(fw))
    if bundle is None or not bundle.load():
        return None
    if _DELEGATE is None:
        # Sparkle asks respondsToSelector: and calls; the signatures are given here, so no protocol is declared.
        class QualmUpdaterDelegate(NSObject):
            # SPUUpdaterDelegate: a test's feed in place of Info.plist's SUFeedURL.
            @objc.typedSelector(b"@@:@")
            def feedURLStringForUpdater_(self, updater):
                return os.environ.get("QUALM_UPDATE_FEED")

            # SPUStandardUserDriverDelegate: a daily check never opens Sparkle's window by
            # itself, not even just after launch (Sparkle's "immediate focus", which for a
            # login item is the moment you log in): a menu line and a corner notice say so,
            # and the window opens when you choose Install… or Check for Updates….
            @objc.typedSelector(b"Z@:")
            def supportsGentleScheduledUpdateReminders(self):
                return True

            @objc.typedSelector(b"Z@:@Z")
            def standardUserDriverShouldHandleShowingScheduledUpdate_andInImmediateFocus_(self, item, focus):
                return False

            @objc.typedSelector(b"v@:Z@@")
            def standardUserDriverWillHandleShowingUpdate_forUpdate_state_(self, handled, item, state):
                if not handled:
                    self.available(str(item.displayVersionString() or item.versionString()))

            @objc.typedSelector(b"v@:@")
            def standardUserDriverDidReceiveUserAttentionForUpdate_(self, item):
                self.attended()

            @objc.typedSelector(b"v@:")
            def standardUserDriverWillFinishUpdateSession(self):
                self.attended()

        _DELEGATE = QualmUpdaterDelegate
    delegate = _DELEGATE.alloc().init()
    delegate.available, delegate.attended = available, attended
    controller = objc.lookUpClass("SPUStandardUpdaterController").alloc() \
        .initWithStartingUpdater_updaterDelegate_userDriverDelegate_(True, delegate, delegate)
    _KEEP.append(delegate)
    return controller
