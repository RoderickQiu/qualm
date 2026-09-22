"""Capture a named app's front window without bringing it to the front, so
trials can run while you keep working. Same walk as state.capture()."""
import time
from AppKit import NSWorkspace
from ApplicationServices import AXUIElementCreateApplication, AXUIElementSetAttributeValue
from seenot_desktop.state import ScreenState, _ax, _clean, _short_url, _walk


def app_by_bundle(bundle_id):
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        if a.bundleIdentifier() == bundle_id:
            return a
    return None


def capture_app(bundle_id) -> ScreenState:
    app = app_by_bundle(bundle_id)
    s = ScreenState(app=str(app.localizedName()), bundle_id=bundle_id)
    root = AXUIElementCreateApplication(app.processIdentifier())
    AXUIElementSetAttributeValue(root, "AXManualAccessibility", True)
    wins = _ax(root, "AXWindows") or []
    if wins:
        s.window_title = _clean(_ax(wins[0], "AXTitle"))
        _walk(wins[0], s)
    s.url = _short_url(s.url)
    return s


def links(bundle_id, pattern):
    import re
    app = app_by_bundle(bundle_id)
    root = AXUIElementCreateApplication(app.processIdentifier())
    q, out, n = [(_ax(root, "AXWindows") or [None])[0]], [], 0
    pat = re.compile(pattern)
    while q and q[0] is not None and n < 8000:
        e = q.pop(0); n += 1
        if _ax(e, "AXRole") == "AXLink":
            u = _ax(e, "AXURL")
            if u is not None and pat.search(str(u)) and str(u) not in out:
                out.append(str(u))
        q.extend(_ax(e, "AXChildren") or [])
    return out
