"""Read the frontmost window as a compact text state.

Jev and Kev read text, not pixels, and Kev was trained on states of at most
384 tokens. So this module does not dump the page: it collects the few
fields that carry most of the signal (app, window title, URL, headings, a
little visible text) and cuts them to a character budget.
"""

from __future__ import annotations

import subprocess
import unicodedata
from dataclasses import asdict, dataclass, field

from AppKit import NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementSetAttributeValue,
)

# Chinese runs about one token per character, English about four characters
# per token. 700 characters keeps a mostly-Chinese state near 384 tokens.
DEFAULT_CHAR_BUDGET = 700
MAX_NODES = 1500  # breadth-first cap on the accessibility tree walk
HEADING_LIMIT = 8
TEXT_LIMIT = 12

BROWSER_APPLESCRIPT = {
    "com.google.Chrome": 'tell application "Google Chrome" to get URL of active tab of front window',
    "com.apple.Safari": 'tell application "Safari" to get URL of front document',
    "company.thebrowser.Browser": 'tell application "Arc" to get URL of active tab of front window',
    "com.microsoft.edgemac": 'tell application "Microsoft Edge" to get URL of active tab of front window',
}


@dataclass
class ScreenState:
    app: str
    bundle_id: str
    window_title: str = ""
    url: str = ""
    headings: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    ax_trusted: bool = True

    def signature(self) -> tuple[str, str, str]:
        """What counts as 'the screen changed' for the watcher."""
        return (self.bundle_id, self.window_title, self.url)

    def to_state(self, char_budget: int = DEFAULT_CHAR_BUDGET) -> dict:
        """The dict sent to the model as `state`, cut to the budget.

        Fields are cut in reverse order of value: body text goes first,
        then headings; app, title and URL are always kept.
        """
        out = {"app": self.app, "window_title": self.window_title}
        if self.url:
            out["url"] = self.url
        used = sum(len(str(v)) for v in out.values())
        for key, items in (("headings", self.headings), ("visible_text", self.text)):
            kept = []
            for item in items:
                if used + len(item) > char_budget:
                    break
                kept.append(item)
                used += len(item)
            if kept:
                out[key] = kept
        return out

    def as_record(self) -> dict:
        return asdict(self)


def _ax(element, attr: str):
    err, value = AXUIElementCopyAttributeValue(element, attr, None)
    return value if err == 0 else None


def _clean(value) -> str:
    if value is None:
        return ""
    # Mail and feed pages pad previews with invisible characters (U+034F,
    # zero-width spaces); they cost tokens and carry nothing.
    s = "".join(ch for ch in str(value) if ch != "\u034f" and unicodedata.category(ch) != "Cf")
    return " ".join(s.split())[:200]


def _walk(window, state: ScreenState) -> None:
    queue = [window]
    seen = 0
    seen_text: set[str] = set()
    while queue and seen < MAX_NODES:
        node = queue.pop(0)
        seen += 1
        role = _ax(node, "AXRole") or ""
        if role == "AXWebArea" and not state.url:
            url = _ax(node, "AXURL")
            if url is not None:
                state.url = _clean(url)
            # The page is what matters; drop the queued tabs and toolbars.
            queue = []
        if role == "AXHeading" and len(state.headings) < HEADING_LIMIT:
            t = _clean(_ax(node, "AXTitle") or _ax(node, "AXDescription") or _ax(node, "AXValue"))
            if t and t not in seen_text:
                seen_text.add(t)
                state.headings.append(t)
        elif role == "AXStaticText" and len(state.text) < TEXT_LIMIT:
            t = _clean(_ax(node, "AXValue"))
            if len(t) >= 4 and t not in seen_text:
                seen_text.add(t)
                state.text.append(t)
        children = _ax(node, "AXChildren")
        if children:
            queue.extend(children)


def _short_url(url: str) -> str:
    # Query strings and fragments are mostly ids: tokens with no signal.
    base = url.split("#", 1)[0]
    if "?" in base and len(base) > 80:
        base = base.split("?", 1)[0]
    return base[:120]


def _browser_url(bundle_id: str) -> str:
    script = BROWSER_APPLESCRIPT.get(bundle_id)
    if not script:
        return ""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=1.5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        return ""


def capture() -> ScreenState:
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    state = ScreenState(
        app=str(app.localizedName() or ""),
        bundle_id=str(app.bundleIdentifier() or ""),
        ax_trusted=bool(AXIsProcessTrusted()),
    )
    if state.ax_trusted:
        root = AXUIElementCreateApplication(app.processIdentifier())
        # Chromium and Electron apps build their web accessibility tree only
        # when a client asks; without this, a browser shows a title and no text.
        AXUIElementSetAttributeValue(root, "AXManualAccessibility", True)
        window = _ax(root, "AXFocusedWindow")
        if window is not None:
            state.window_title = _clean(_ax(window, "AXTitle"))
            _walk(window, state)
    if not state.url:
        state.url = _browser_url(state.bundle_id)
    state.url = _short_url(state.url)
    return state
