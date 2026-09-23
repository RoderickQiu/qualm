"""Read the frontmost window as a compact text state.

Jev and Kev read text, not pixels, and Kev was trained on states of at most
384 tokens. So this module does not dump the page: it collects the few
fields that carry most of the signal (app, window title, URL, headings, a
little visible text) and cuts them to a character budget.
"""

from __future__ import annotations

import re
import subprocess
import time
import unicodedata
from dataclasses import asdict, dataclass, field

from AppKit import NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementSetAttributeValue,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)

# Chinese runs about one token per character, English about four characters
# per token. 700 characters keeps a mostly-Chinese state near 384 tokens.
DEFAULT_CHAR_BUDGET = 700
MAX_NODES = 1500  # breadth-first cap on the accessibility tree walk
HEADING_LIMIT = 8
TEXT_LIMIT = 12
OCR_BELOW = 20  # characters of AX text under which a non-browser window is read from its picture (ocr.py)
FIRST_WAIT_S = 0.3  # a Chromium/Electron tree switched on just now: one more look after this

# Safari's AutoFill popover sits in the window's AX tree outside the page, and
# stayed there for pages after a sign-in ("Apple Account / Continue with Touch
# ID / <your name>"), making them look like login pages. The walk drops what's
# outside the page once it finds one; these lines are dropped from windows
# where it doesn't.
AUTOFILL_LINES = ("Continue with Touch ID", "Apple Account", "Apple 账户")
AUTOFILL_PREFIXES = ("Sign in to your Apple Account for ",)
SKIP_ROLES = ("AXMenu", "AXMenuBar", "AXPopover")  # never the page: menus and popovers
# Terminals, editors and notes keep their text in text areas, which the walk
# doesn't read; a window with one is never pictured for OCR (it could hold
# anything you typed, and with the hosted model the text would leave the Mac).
TEXT_AREA_ROLES = ("AXTextArea", "AXTextView")

BROWSER_UI = ("chrome://", "chrome-extension://", "chrome-untrusted://", "edge://", "about:", "devtools://")

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
    frame: list[float] = field(default_factory=list)  # window x, y, w, h in points, for the review screenshot
    ocr: bool = False  # `text` was read from a picture of the window, not the AX tree

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
        d = asdict(self)
        if not d["ocr"]:
            del d["ocr"]  # records from before OCR stay byte for byte the same
        return d


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


def _title(title: str, app: str) -> str:
    """The window title without what the browser adds: its own name (the
    state has the app already), Chrome's "High memory usage - 1.2 GB", whose
    number changes as you read and looked like a new page to the watcher,
    and unread counts ("(3) Inbox"), which change without the page changing."""
    t = re.sub(r" - High memory usage - [\d.]+ [KMG]B", "", title)
    t = re.sub(r"^\(\d+\+?\) ", "", t)
    if app:
        # "Page - Google Chrome", "Page - Google Chrome (Incognito)", "Page - Google Chrome - Work"
        t = re.sub(rf" [-–—] {re.escape(app)}( \([^)]*\))?( [-–—] [^-–—]+)?$", "", t)
    return t or title


def _walk(window, state: ScreenState) -> int:
    """Breadth first. What's found before the page (toolbars, a popover left
    open) is dropped when the page turns up: only the page's text is kept.
    Returns how many text areas it passed (see TEXT_AREA_ROLES)."""
    queue = [window]
    seen = areas = 0
    seen_text: set[str] = set()
    in_page = False
    while queue and seen < MAX_NODES:
        node = queue.pop(0)
        seen += 1
        role = _ax(node, "AXRole") or ""
        if role in SKIP_ROLES and not in_page:
            continue
        areas += role in TEXT_AREA_ROLES
        if role == "AXWebArea" and not in_page:
            url = _clean(_ax(node, "AXURL"))
            if url.startswith(BROWSER_UI):
                # Chrome's own UI (the address-bar dropdown, extension popups)
                # is a web area too; skip it and keep looking for the page.
                continue
            state.url = state.url or url
            # The page is what matters; drop the queued tabs and toolbars, and
            # anything read from them.
            in_page, queue = True, []
            state.headings, state.text, seen_text = [], [], set()
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
    if not in_page:
        state.headings = [t for t in state.headings if not _autofill(t)]
        state.text = [t for t in state.text if not _autofill(t)]
    return areas


def _autofill(line: str) -> bool:
    return line in AUTOFILL_LINES or line.startswith(AUTOFILL_PREFIXES)


def _chars(state: ScreenState) -> int:
    return sum(map(len, state.headings)) + sum(map(len, state.text))


def _frame(window) -> list[float]:
    pos, size = _ax(window, "AXPosition"), _ax(window, "AXSize")
    if pos is None or size is None:
        return []
    ok1, p = AXValueGetValue(pos, kAXValueCGPointType, None)
    ok2, s = AXValueGetValue(size, kAXValueCGSizeType, None)
    return [p.x, p.y, s.width, s.height] if ok1 and ok2 else []


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


_switched_on: set[int] = set()  # pids whose web accessibility tree capture() has asked for


def capture(skip: tuple[str, ...] = ()) -> ScreenState:
    """The front window. Apps in `skip` (settings.no_monitor) are named but
    never read: no title, no URL, no text."""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    state = ScreenState(
        app=str(app.localizedName() or ""),
        bundle_id=str(app.bundleIdentifier() or ""),
        ax_trusted=bool(AXIsProcessTrusted()),
    )
    if state.bundle_id in skip:
        return state
    if state.ax_trusted:
        pid = app.processIdentifier()
        root = AXUIElementCreateApplication(pid)
        # Chromium and Electron apps build their web accessibility tree only
        # when a client asks; without this, a browser shows a title and no text.
        AXUIElementSetAttributeValue(root, "AXManualAccessibility", True)
        first = pid not in _switched_on
        _switched_on.add(pid)
        window = _ax(root, "AXFocusedWindow")
        if window is not None:
            state.window_title = _title(_clean(_ax(window, "AXTitle")), state.app)
            state.frame = _frame(window)
            areas = _walk(window, state)
            if first and not _chars(state):
                # Asked just now: the tree appears a moment later (the set
                # call itself returns -25205). One more look, once per process.
                time.sleep(FIRST_WAIT_S)
                state.headings, state.text, state.url = [], [], ""
                areas = _walk(window, state)
            if (_chars(state) < OCR_BELOW and not areas and not state.url
                    and state.bundle_id not in BROWSER_APPLESCRIPT):
                _read_pixels(state, pid)
    if not state.url:
        state.url = _browser_url(state.bundle_id)
    state.url = _short_url(state.url)
    return state


def _read_pixels(state: ScreenState, pid: int) -> None:
    """A window that draws its text (a video player, a canvas): read it from a picture."""
    from .ocr import window_text

    try:
        lines = window_text(pid, state.frame, state.window_title)
    except Exception:  # no Screen Recording, Vision unavailable: keep what AX gave
        return
    seen = set(state.headings) | set(state.text)
    title = state.window_title.replace(" ", "")
    extra = []
    for line in lines:
        t = _clean(line)
        if t and t not in seen and t.replace(" ", "") != title:  # the title bar, read back
            seen.add(t)
            extra.append(t)
    if extra:
        state.text = (state.text + extra)[:TEXT_LIMIT]
        state.ocr = True
