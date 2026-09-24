"""Are you at the screen? Check-in sessions only count time while you are.

Away means the screen is locked, or there has been no keyboard or mouse
input for IDLE_S, unless the front app is keeping the display awake (a
video playing: you watch without touching anything). Keep-awake tools
like Caffeine hold that assertion all the time, so only the front app's
own assertions count.
"""

from __future__ import annotations

import re
import subprocess
import time

import Quartz

IDLE_S = 120  # no input for this long and nothing playing: away
MEDIA_CHECK_S = 30  # `pmset -g assertions` is run at most this often


def idle_seconds() -> float:
    return float(Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType))


def screen_locked() -> bool:
    d = Quartz.CGSessionCopyCurrentDictionary() or {}
    return bool(d.get("CGSSessionScreenIsLocked", False))


def display_kept_awake_by(*names: str) -> bool:
    """True if the app called any of `names` (or one of its helpers, e.g.
    "Google Chrome Helper") holds a PreventUserIdleDisplaySleep assertion:
    typically, video playing. pmset names processes by their executable
    ("Safari"), not by the name the app shows, which can be translated
    ("Safari浏览器" in Chinese): give both."""
    try:
        out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    # "pid 99(Google Chrome Helper (Renderer)): [0x..] ... PreventUserIdleDisplaySleep ..."
    owners = re.findall(r"pid \d+\((.*?)\): \[.*PreventUserIdleDisplaySleep", out)
    wanted = {n.strip("‎").lower() for n in names} - {""}
    return any(o.lower().startswith(n) for o in owners for n in wanted)


class Presence:
    def __init__(self, idle_s: float = IDLE_S):
        self.idle_s = idle_s
        self._media: tuple[float, tuple[str, ...], bool] = (0.0, (), False)  # checked at, app, result

    def away(self, *front_app: str) -> bool:
        """`front_app`: the front app's names (as shown, and its executable)."""
        if screen_locked():
            return True
        if idle_seconds() < self.idle_s:
            return False
        at, app, playing = self._media
        now = time.monotonic()
        if app != front_app or now - at > MEDIA_CHECK_S:
            playing = display_kept_awake_by(*front_app)
            self._media = (now, front_app, playing)
        return not playing
