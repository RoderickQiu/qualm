"""Read the frontmost window as a compact text state.

Jev and Kev read text, not pixels, and Kev was trained on states of at most
384 tokens. So this module does not dump the page: it collects the few
fields that carry most of the signal (app, window title, URL, headings, a
little visible text) and cuts them to a character budget.
"""

from __future__ import annotations

import os
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

from .rules import PASSWORD_MANAGERS, app_listed

# Chinese runs about one token per character, English about four characters
# per token. 700 characters keeps a mostly-Chinese state near 384 tokens.
DEFAULT_CHAR_BUDGET = 700
MAX_NODES = 1500  # breadth-first cap on the accessibility tree walk, and on the page's own
UI_NODES = 500  # the cap inside the browser's own web areas (side panels, Vivaldi's window) when looking for the page
# A node with more children than this, in a tab strip or the browser's own web
# area, is a list (tabs, History): walked last when looking for the page.
WIDE = 30
HEADING_LIMIT = 8
TEXT_LIMIT = 12
OCR_BELOW = 20  # characters of AX text under which a window may be read from its picture (ocr.py; see capture())
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

# A browser's own pages, never the page you're on. Vivaldi's whole window is
# one (chrome-extension://.../browser.html), with the page inside it.
BROWSER_UI = ("chrome://", "chrome-extension://", "chrome-untrusted://", "chrome-search://", "edge://", "brave://",
              "vivaldi://", "opera://", "arc://", "about:", "devtools://", "moz-extension://",
              "safari-web-extension://")

# Browsers by bundle id. The Chromium ones speak Chrome's AppleScript (URL
# and "go back" on the active tab), or get keys where they don't; Safari has
# its own. The others are driven by keys: Cmd-[ is Back and Cmd-T a new tab
# in every one of them. A browser missing here is still found by its window
# (browser_window) and gets one Back.
CHROMIUM = {
    "com.google.Chrome", "com.google.Chrome.beta", "com.google.Chrome.dev", "com.google.Chrome.canary",
    "org.chromium.Chromium", "com.brave.Browser", "com.brave.Browser.beta", "com.brave.Browser.nightly",
    "com.microsoft.edgemac", "com.microsoft.edgemac.beta", "com.microsoft.edgemac.dev", "com.microsoft.edgemac.canary",
    "com.vivaldi.Vivaldi", "com.vivaldi.Vivaldi.snapshot", "com.operasoftware.Opera", "com.operasoftware.OperaGX",
    "com.operasoftware.OperaNext", "com.operasoftware.OperaDeveloper", "company.thebrowser.Browser",
    "company.thebrowser.dia", "ai.perplexity.comet", "com.openai.atlas", "ru.yandex.desktop.yandex-browser",
    "com.naver.Whale", "com.tencent.qqbrowserappmac", "com.citrolabs.ego.lite",
}
SAFARI = "com.apple.Safari"
BY_KEYS = {
    "org.mozilla.firefox", "org.mozilla.firefoxdeveloperedition", "org.mozilla.nightly", "app.zen-browser.zen",
    "io.gitlab.librewolf-community", "net.waterfox.waterfox", "one.ablaze.floorp", "org.torproject.torbrowser",
    "net.mullvad.mullvadbrowser", "com.kagi.kfmac", "com.duckduckgo.macos.browser", "com.apple.SafariTechnologyPreview",
    "com.sigmaos.sigmaos.macos",
}
BROWSERS = CHROMIUM | {SAFARI} | BY_KEYS


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


# Chrome's memory label, which it adds to window titles in its own language:
# "Page - High memory usage - 1.2 GB", "Seite – Hohe Arbeitsspeichernutzung –
# 1,2 GB", "页面 - 内存用量高 - 1.2 GB", "Страница – използвана памет: 345 МБ".
# Matched by its shape and Chrome's units, from Chrome's own strings in all 55
# of its languages. Spanish, Brazilian Portuguese and Russian put the title
# inside the sentence, and keep it.
MEMORY_SIZE = r"\d[\d.,]*\s?(?:[KMG]B|[KMG]o|[KMG]t|[КМГ]Б|ميغابايت|غيغابايت|مگابایت|گیگابایت|एमबी|जीबी|এমবি|জিবি|மெ\.பை|ሜባ|ጊባ)"
MEMORY_LABEL = re.compile(rf" [-–—] [^\d:\-–—]{{1,60}}?(?: ?[-–—] ?|: ){MEMORY_SIZE}(?=$| [-–—] )")


def _title(title: str, app: str, bundle_id: str = "") -> str:
    """The window title without what the browser adds: its own name (the
    state has the app already), a Chromium browser's memory label ("High
    memory usage - 1.2 GB"), whose number changes as you read and looked
    like a new page to the watcher, and unread counts ("(3) Inbox"), which
    change without the page changing. Only Chromium adds that label: in any
    other app " - Red - 128 GB" is part of the title (a product, a file)."""
    t = MEMORY_LABEL.sub("", title) if bundle_id in CHROMIUM else title
    t = re.sub(r"^\(\d+\+?\) ", "", t)
    if app:
        # "Page - Google Chrome", "Page - Google Chrome (Incognito)", "Page - Google Chrome - Work"
        t = re.sub(rf" [-–—] {re.escape(app)}( \([^)]*\))?( [-–—] [^-–—]+)?$", "", t)
    return t or title


def _read(node, role: str, state: ScreenState, seen_text: set[str]) -> None:
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


def _area(node) -> float:
    f = _frame(node)
    return f[2] * f[3] if f else 0.0


def _page(window, state: ScreenState | None = None) -> tuple:
    """The page in a window, breadth first: of its web areas, one with an
    address, and the largest. A browser's own UI can be a web area with the
    page inside it (Vivaldi's whole window; Chrome's address-bar dropdown),
    and a sidebar one beside it (a chat panel): neither is the page. The
    window outside the browser's own web areas is walked first; inside them
    only while no page with an address has turned up, and for at most
    UI_NODES, so a long History side panel can't use up the walk. Lists
    (a tab strip, or anything over WIDE items in the browser's own web
    area) are walked last, only while the page hasn't turned up, and a tab
    is never walked into: hundreds of tabs leave the page its budget. With
    `state`, the text met before any page is read into it. Returns (the page
    or None, its address, text areas passed, whether it met a web area)."""
    pages, inside = [], []
    areas, web = 0, False
    seen_text: set[str] = set()
    for queue, limit in (([window], MAX_NODES), (inside, UI_NODES)):
        if any(url for _, url in pages):
            break
        seen, lists = 0, []
        while seen < limit:
            if not queue:
                if not lists or any(url for _, url in pages):
                    break
                queue.append(lists.pop(0))
            node = queue.pop(0)
            seen += 1
            role = _ax(node, "AXRole") or ""
            if role in SKIP_ROLES:
                continue
            areas += role in TEXT_AREA_ROLES
            if role == "AXWebArea":
                web = True
                url = _clean(_ax(node, "AXURL"))
                if not url.startswith(BROWSER_UI):
                    pages.append((node, url))
                    continue  # its text is read if it's the page
                if queue is not inside:  # the browser's own page: looked inside after the rest
                    inside.extend(_ax(node, "AXChildren") or [])
                    continue
            if state is not None and not pages:
                _read(node, role, state, seen_text)
            if role == "AXRadioButton":  # a tab: its title and close button, never the page
                continue
            kids = _ax(node, "AXChildren") or []
            wide = len(kids) > WIDE and (role == "AXTabGroup" or queue is inside)
            (lists if wide else queue).extend(kids)
    if not pages:
        return None, "", areas, web
    page, url = pages[0] if len(pages) == 1 else max(pages, key=lambda p: (bool(p[1]), _area(p[0])))
    return page, url, areas, web


def _walk(window, state: ScreenState) -> tuple[int, bool]:
    """The page's text (see _page), with a walk of its own; what was read
    before it (toolbars, a popover left open) is dropped. With no page, the
    window's text, less Safari's AutoFill. Returns how many text areas it
    passed (see TEXT_AREA_ROLES), and whether it met a web area."""
    page, url, areas, web = _page(window, state)
    if page is None:
        state.headings = [t for t in state.headings if not _autofill(t)]
        state.text = [t for t in state.text if not _autofill(t)]
        return areas, web
    state.url = state.url or url
    state.headings, state.text = [], []
    seen_text: set[str] = set()
    queue, seen = [page], 0
    while queue and seen < MAX_NODES:
        node = queue.pop(0)
        seen += 1
        role = _ax(node, "AXRole") or ""
        areas += role in TEXT_AREA_ROLES
        _read(node, role, state, seen_text)
        queue.extend(_ax(node, "AXChildren") or [])
    return areas, web


def page_address(pid: int, title: bool = False) -> str | None:
    """The address of the page in `pid`'s front window, read through
    Accessibility (for Take me back in a browser Qualm can't script); with
    `title`, the window's title instead. None if there's none to read."""
    window = _ax(AXUIElementCreateApplication(pid), "AXFocusedWindow")
    if window is None:
        return None
    return (_clean(_ax(window, "AXTitle")) if title else _page(window)[1]) or None


def browser_window(pid: int) -> bool:
    """Whether `pid`'s front window looks like a web browser's, for Take me
    back in one Qualm doesn't list (BROWSERS): a page with an http(s)
    address, and outside the page a tab strip or an address field showing
    its site. A web app's window (Slack, an Electron app) has the page but
    neither: its tabs, if any, are inside the page."""
    window = _ax(AXUIElementCreateApplication(pid), "AXFocusedWindow")
    if window is None:
        return False
    m = re.match(r"https?://(?:www\.)?([^/:?#]+)", _page(window)[1])
    if not m:
        return False
    host, queue, seen = m.group(1).lower(), [window], 0
    while queue and seen < MAX_NODES:
        node = queue.pop(0)
        seen += 1
        role = _ax(node, "AXRole") or ""
        if role == "AXTabGroup":
            return True
        if role in ("AXTextField", "AXComboBox") and host in _clean(_ax(node, "AXValue")).lower():
            return True
        page = role == "AXWebArea" and not _clean(_ax(node, "AXURL")).startswith(BROWSER_UI)
        if page or role in ("AXRadioButton", *SKIP_ROLES):
            continue  # the page itself, a tab, a menu
        queue.extend(_ax(node, "AXChildren") or [])
    return False


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
    if bundle_id in CHROMIUM:
        script = f'tell application id "{bundle_id}" to get URL of active tab of front window'
    elif bundle_id == SAFARI:
        script = f'tell application id "{bundle_id}" to get URL of front document'
    else:
        return ""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=1.5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        return ""


_switched_on: set[int] = set()  # pids whose web accessibility tree capture() has asked for


def capture(skip: tuple[str, ...] = ()) -> ScreenState:
    """The front window. Password managers and the apps in `skip`
    (settings.no_monitor, bundle ids or names) are named but never read: no
    title, no URL, no text."""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    state = ScreenState(
        app=str(app.localizedName() or ""),
        bundle_id=str(app.bundleIdentifier() or ""),
        ax_trusted=bool(AXIsProcessTrusted()),
    )
    if app_listed((*PASSWORD_MANAGERS, *skip), state.bundle_id, state.app):
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
            state.window_title = _title(_clean(_ax(window, "AXTitle")), state.app, state.bundle_id)
            state.frame = _frame(window)
            areas, web = _walk(window, state)
            if first and not _chars(state):
                # Asked just now: the tree appears a moment later (the set
                # call itself returns -25205). One more look, once per process.
                time.sleep(FIRST_WAIT_S)
                state.headings, state.text, state.url = [], [], ""
                areas, web = _walk(window, state)
            # A browser's page is in its web tree, however little text it has;
            # one with no web tree at all (Firefox with its accessibility
            # switched off) is read from a picture like any other window.
            if (_chars(state) < OCR_BELOW and not areas and not state.url
                    and (state.bundle_id not in BROWSERS or not web)):
                _read_pixels(state, pid)
    if not state.url:
        state.url = _browser_url(state.bundle_id)
    state.url = _short_url(state.url)
    return state


def installed_app(name: str) -> tuple[str, str] | None:
    """(bundle id, name) of the installed app with this name, bundle id or
    path, in any case ("figma", "Figma.app", "com.figma.Desktop",
    "/Applications/Figma.app"); None if there's none."""
    from Foundation import NSBundle

    ws, name = NSWorkspace.sharedWorkspace(), name.strip()
    url = ws.URLForApplicationWithBundleIdentifier_(name)
    if url is not None:
        path = url.path()
    elif "/" in name:  # dragged from Finder into a terminal
        path = os.path.expanduser(name)
    else:
        path = ws.fullPathForApplication_(name.removesuffix(".app"))
    bundle = NSBundle.bundleWithPath_(path) if path else None
    bid = bundle.bundleIdentifier() if bundle is not None else None
    return (str(bid), str(path).rstrip("/").rsplit("/", 1)[-1].removesuffix(".app")) if bid else None


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
