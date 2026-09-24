"""The first-run window: a short setup assistant, one question per page.

    Welcome -> Where the model runs -> Permission -> What to watch for -> Ready

Shown by the app until setup has run for this Qualm folder (setup.needed():
no .setup-done mark, no judgements, no model chosen, no rules.toml an
earlier version saved).
Every choice goes through setup.apply, the same as `qualm setup`. Each option
says what it costs in plain numbers: memory and download for the local model;
what leaves the Mac, the account and the price for the hosted one. Each
permission says what it's for (Screen Recording is optional).

Laid out with Auto Layout (stack views), in the system's colors, so it follows
light and dark mode and the accent color you picked.
"""

from __future__ import annotations

import platform
import subprocess
import threading
from pathlib import Path

import objc
from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSBox,
    NSBoxCustom,
    NSBoxSeparator,
    NSButton,
    NSColor,
    NSFont,
    NSImage,
    NSImageSymbolConfiguration,
    NSImageView,
    NSLayoutAttributeCenterX,
    NSLayoutAttributeCenterY,
    NSLayoutAttributeLeading,
    NSLayoutAttributeTop,
    NSLayoutConstraint,
    NSMakeRect,
    NSProgressIndicator,
    NSSecureTextField,
    NSStackView,
    NSStackViewGravityLeading,
    NSStackViewGravityTrailing,
    NSSwitch,
    NSTextAlignmentCenter,
    NSTextField,
    NSTimer,
    NSUserInterfaceLayoutOrientationHorizontal,
    NSUserInterfaceLayoutOrientationVertical,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from . import autostart, keychain, localmodel, paths
from . import setup as s
from .watcher import SETUP_TITLE

W, H = 640.0, 560.0  # content size
SIDE = 48.0  # left and right margin
TEXT_W = W - 2 * SIDE
BAR_H = 64.0
KEY_URL = "https://console.typesafe.ai"
ICON = Path(__file__).resolve().parent / "icon.png"
# docs/MODELS.md: $0.02-0.06 a workday, $0.50-1.25 a month, at the early-access price.
DOWNLOAD_GB = localmodel.DOWNLOAD_GB + localmodel.RUNTIME_GB  # the model and its runtime, the first time
HOSTED_COST = "at TypeSafe's early-access price that's about $1 a month"
SCREEN_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
# Opened from the disk image (or a copy macOS runs from a temporary folder): the setup window's words for
# autostart.MOVE_FIRST, short enough to sit above the buttons.
MOVE_FIRST = ("Qualm is running from its disk image: drag it to Applications and open it from there, so it can "
              "start when you log in.")


def screen_pane() -> str:
    """Screen Recording's pane as System Settings names it: macOS 15 added system audio to it."""
    mac = platform.mac_ver()[0]
    return "Screen & System Audio Recording" if not mac or int(mac.split(".")[0]) >= 15 else "Screen Recording"


def key_trouble(err: str) -> str:
    """What a failed key check (setup.check_key's "ErrorClass: message") means, in words."""
    if "Authentication" in err or "PermissionDenied" in err:
        return "That key didn't work. Check it and try again."
    if "Connection" in err or "Timeout" in err:
        return "Couldn't reach TypeSafe. Check your internet connection and try again."
    if "RateLimit" in err:
        return "TypeSafe says this key has reached its usage limit. Try another key, or try again later."
    return "TypeSafe couldn't check the key just now. Try again in a minute."


def screen_recording() -> bool:
    """Screen Recording: for windows that draw their text as pixels (read from a
    picture of the window), and the screenshots on the review page."""
    from Quartz import CGPreflightScreenCaptureAccess

    return bool(CGPreflightScreenCaptureAccess())


def ask_screen_recording() -> None:
    """macOS's own prompt (it adds Qualm to the list), and the settings pane."""
    from Quartz import CGRequestScreenCaptureAccess

    CGRequestScreenCaptureAccess()
    subprocess.run(["open", SCREEN_PANE], check=False)

# -- small builders ---------------------------------------------------------------

def _fixed(view, width=None, height=None):
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    if width is not None:
        view.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
    if height is not None:
        view.heightAnchor().constraintEqualToConstant_(height).setActive_(True)
    return view


def _pin(view, parent, top=None, bottom=None, leading=None, trailing=None):
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    cs = []
    if top is not None:
        cs.append(view.topAnchor().constraintEqualToAnchor_constant_(parent.topAnchor(), top))
    if bottom is not None:
        cs.append(view.bottomAnchor().constraintEqualToAnchor_constant_(parent.bottomAnchor(), -bottom))
    if leading is not None:
        cs.append(view.leadingAnchor().constraintEqualToAnchor_constant_(parent.leadingAnchor(), leading))
    if trailing is not None:
        cs.append(view.trailingAnchor().constraintEqualToAnchor_constant_(parent.trailingAnchor(), -trailing))
    NSLayoutConstraint.activateConstraints_(cs)
    return view


def _text(value, size=13.0, weight=0.0, color=None, width=None, center=False):
    """A label; with `width`, it wraps to that width."""
    f = NSTextField.wrappingLabelWithString_(value) if width else NSTextField.labelWithString_(value)
    f.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    f.setTextColor_(color or NSColor.labelColor())
    f.setSelectable_(False)
    if center:
        f.setAlignment_(NSTextAlignmentCenter)
    if width:
        f.setPreferredMaxLayoutWidth_(width)
        _fixed(f, width=width)
    return f


def _symbol(name, size=15.0, color=None, weight=0.0):
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    v = NSImageView.imageViewWithImage_(img.imageWithSymbolConfiguration_(
        NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, weight)))
    v.setContentTintColor_(color or NSColor.secondaryLabelColor())
    return v


def _stack(views, vertical=True, spacing=8.0, align=None):
    st = NSStackView.stackViewWithViews_(views)
    st.setOrientation_(NSUserInterfaceLayoutOrientationVertical if vertical else NSUserInterfaceLayoutOrientationHorizontal)
    st.setSpacing_(spacing)
    st.setAlignment_(align if align is not None else (NSLayoutAttributeLeading if vertical else NSLayoutAttributeCenterY))
    st.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return st


def _box(radius=10.0, fill=None, border=None, width=1.0):
    b = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
    b.setBoxType_(NSBoxCustom)
    b.setTitlePosition_(0)  # no title
    b.setCornerRadius_(radius)
    b.setBorderWidth_(width if border is not None else 0)
    if border is not None:
        b.setBorderColor_(border)
    b.setFillColor_(fill or NSColor.clearColor())
    b.setContentViewMargins_((0, 0))
    b.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return b


def _fill_box(box, content, inset=16.0):
    box.contentView().addSubview_(content)
    _pin(content, box.contentView(), top=inset, bottom=inset, leading=inset, trailing=inset)
    return box


def _separator(width):
    b = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, width, 1))
    b.setBoxType_(NSBoxSeparator)
    return _fixed(b, width=width)


def _tile(symbol, size=30.0):
    """An SF Symbol on a soft accent square: the icon of a list row."""
    box = _fixed(_box(radius=7, fill=NSColor.controlAccentColor().colorWithAlphaComponent_(0.14)), size, size)
    icon = _symbol(symbol, size * 0.45, NSColor.controlAccentColor(), 0.2)
    icon.setTranslatesAutoresizingMaskIntoConstraints_(False)
    box.contentView().addSubview_(icon)
    NSLayoutConstraint.activateConstraints_([
        icon.centerXAnchor().constraintEqualToAnchor_(box.centerXAnchor()),
        icon.centerYAnchor().constraintEqualToAnchor_(box.centerYAnchor())])
    return box


class QualmChoiceCard(NSBox):
    """A card you click to choose: the model's two homes."""

    def mouseDown_(self, event):
        self.on_pick()

    def acceptsFirstMouse_(self, event):
        return True


# -- the assistant ---------------------------------------------------------------

class Onboarding(NSObject):
    @objc.python_method
    def setup(self, on_done, show: bool = True):
        self.on_done = on_done
        self.rec, self.why = s.recommend()
        self.has_key = bool(keychain.api_key())
        self.backend = self.rec
        self.page = 0
        self._build()
        self._choose(self.rec)
        self._go(0)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "tick:", None, True)
        if show:
            self.window.center()
            NSApp.activateIgnoringOtherApps_(True)
            self.window.makeKeyAndOrderFront_(None)
        return self

    @objc.python_method
    def _build(self):
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskFullSizeContentView
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        self.window.setTitle_(SETUP_TITLE)  # the watcher skips windows with this title
        self.window.setTitleVisibility_(NSWindowTitleHidden)
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setMovableByWindowBackground_(True)
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        root = self.window.contentView()

        self.pages = [self._welcome(), self._model(), self._permission(), self._rules(), self._ready()]
        for p in self.pages:
            root.addSubview_(p)
            _pin(p, root, top=0, leading=0, trailing=0, bottom=BAR_H)
        self._bar(root)

    # -- pages --------------------------------------------------------------------

    @objc.python_method
    def _page(self, step, title, subtitle, *body, spacing=22.0):
        """A page: step, title and subtitle at the top, then the body, left-aligned."""
        head = _stack([_text(f"STEP {step} OF 4", 11, 0.5, NSColor.controlAccentColor()),
                       _text(title, 24, 0.4, width=TEXT_W),
                       _text(subtitle, 13, 0.0, NSColor.secondaryLabelColor(), width=TEXT_W)], spacing=6)
        head.setCustomSpacing_afterView_(8, head.views()[0])
        page = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H - BAR_H))
        col = _stack([head, *body], spacing=spacing)
        page.addSubview_(col)
        _pin(col, page, top=52, leading=SIDE, trailing=SIDE)
        return page

    @objc.python_method
    def _welcome(self):
        icon = NSImageView.imageViewWithImage_(NSImage.alloc().initWithContentsOfFile_(str(ICON)))
        _fixed(icon, 96, 96)
        features = _stack([
            self._feature("eye", "Notices the trap, not the site",
                          "Endless feeds, short videos and livestreams. Not the lecture, or the thread you searched for."),
            self._feature("hand.raised", "Steps in gently",
                          "A pause, a question and a way back. It never locks you out."),
            self._feature("slider.horizontal.3", "Yours to shape",
                          "Rules in plain words. Switch them on and off from the menu bar; change the wording "
                          "with your AI agent."),
        ], spacing=16)
        col = _stack([icon,
                      _text("Welcome to Qualm", 28, 0.4),
                      _text("A second thought before the scroll.", 15, 0.0, NSColor.secondaryLabelColor()),
                      features], spacing=10, align=NSLayoutAttributeCenterX)
        col.setCustomSpacing_afterView_(14, icon)
        col.setCustomSpacing_afterView_(34, col.views()[2])
        page = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H - BAR_H))
        page.addSubview_(col)
        _pin(col, page, top=56)
        col.centerXAnchor().constraintEqualToAnchor_(page.centerXAnchor()).setActive_(True)
        return page

    @objc.python_method
    def _feature(self, symbol, title, detail):
        words = _stack([_text(title, 13, 0.3), _text(detail, 12, 0.0, NSColor.secondaryLabelColor(), width=380)],
                       spacing=2)
        row = _stack([_tile(symbol, 34), words], vertical=False, spacing=14)
        row.setAlignment_(NSLayoutAttributeCenterY)
        return row

    @objc.python_method
    def _model(self):
        card_w = (TEXT_W - 16) / 2
        self.card_local = self._card(card_w, "laptopcomputer", "On this Mac", "Private: nothing leaves your Mac", [
            ("bolt", "About 1 s per check"),
            ("memorychip", f"{localmodel.NEED_GB[0]:.0f}–{localmodel.NEED_GB[1]:.0f} GB of memory while running"),
            ("arrow.down.circle", f"About {DOWNLOAD_GB:.0f} GB download, once"),
            ("checkmark.circle", "Free, no account needed"),
        ], "kev")
        self.card_hosted = self._card(card_w, "cloud", "Hosted", "By TypeSafe, about $1 a month", [
            ("bolt", "About 0.2 s per check"),
            ("memorychip", "Almost no memory"),
            ("paperplane", "Screen text is sent to TypeSafe"),
            ("key", "Needs a TypeSafe account and key"),
        ], "jev")
        cards = _stack([self.card_local, self.card_hosted], vertical=False, spacing=16)
        cards.setAlignment_(NSLayoutAttributeTop)
        self.card_local.heightAnchor().constraintEqualToAnchor_(self.card_hosted.heightAnchor()).setActive_(True)

        note = _stack([_symbol("info.circle", 12), _text(self.why, 12, 0.0, NSColor.secondaryLabelColor(),
                                                            width=TEXT_W - 22)], vertical=False, spacing=8)
        note.setAlignment_(NSLayoutAttributeTop)

        # The key, only for the hosted model.
        if self.has_key:
            key_row = _stack([_symbol("checkmark.circle.fill", 13, NSColor.systemGreenColor()),
                              _text("Using the TypeSafe key saved on this Mac.", 12, 0.0, NSColor.secondaryLabelColor())],
                             vertical=False, spacing=8)
            self.key = None
        else:
            self.key = _fixed(NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 24)), width=320)
            self.key.setPlaceholderString_("TypeSafe API key")
            get = NSButton.buttonWithTitle_target_action_("Get a key", self, "getKey:")
            get.setBordered_(False)
            get.setContentTintColor_(NSColor.linkColor())
            key_row = _stack([self.key, get], vertical=False, spacing=10)
        self.key_row = key_row
        self.model_error = _text("", 12, 0.0, NSColor.secondaryLabelColor(), width=TEXT_W)
        return self._page(1, "Where should the model run?",
                          "Each time the screen changes, Qualm asks a small language model what's on it. "
                          "You can switch later.", cards, note, key_row, self.model_error, spacing=16)

    @objc.python_method
    def _card(self, width, symbol, title, subtitle, facts, backend):
        card = QualmChoiceCard.alloc().initWithFrame_(NSMakeRect(0, 0, width, 10))
        card.setBoxType_(NSBoxCustom)
        card.setTitlePosition_(0)
        card.setCornerRadius_(12)
        card.setContentViewMargins_((0, 0))
        card.on_pick = lambda: self._choose(backend)
        _fixed(card, width=width)
        icon = _symbol(symbol, 22, NSColor.secondaryLabelColor(), 0.2)
        top = _stack([icon], vertical=False, spacing=6)
        if backend == self.rec:
            pill = _fixed(_box(radius=9, fill=NSColor.controlAccentColor().colorWithAlphaComponent_(0.16)), 96, 18)
            lbl = _text("Recommended", 10.5, 0.5, NSColor.controlAccentColor())
            lbl.setTranslatesAutoresizingMaskIntoConstraints_(False)
            pill.contentView().addSubview_(lbl)
            NSLayoutConstraint.activateConstraints_([
                lbl.centerXAnchor().constraintEqualToAnchor_(pill.centerXAnchor()),
                lbl.centerYAnchor().constraintEqualToAnchor_(pill.centerYAnchor())])
            top.addView_inGravity_(pill, NSStackViewGravityTrailing)
        _fixed(top, width=width - 36)
        rows = [_stack([_symbol(sym, 11), _text(t, 12, 0.0, NSColor.secondaryLabelColor())], vertical=False, spacing=8)
                for sym, t in facts]
        body = _stack([top, _text(title, 15, 0.4), _text(subtitle, 12, 0.0, NSColor.secondaryLabelColor()),
                       _stack(rows, spacing=6)], spacing=4)
        body.setCustomSpacing_afterView_(12, top)
        body.setCustomSpacing_afterView_(14, body.views()[2])
        _fill_box(card, body, inset=18)
        card.icon = icon
        if backend == "kev" and (why := localmodel.unsupported()):  # the note under the cards says why
            card.on_pick = lambda: None
            card.setAlphaValue_(0.45)
            card.setToolTip_(why)
        return card

    @objc.python_method
    def _permission(self):
        self.perm_icon = _symbol("lock.fill", 26, NSColor.secondaryLabelColor(), 0.2)
        _fixed(self.perm_icon, 34, 34)
        self.perm_title = _text("Accessibility is off", 15, 0.4)
        who = "Qualm" if paths.bundle() else "the app you run Qualm from (your terminal)"
        self.perm_ask = f"In System Settings › Privacy & Security › Accessibility, turn on {who}."
        self.perm_detail = _text(self.perm_ask, 12, 0.0, NSColor.secondaryLabelColor(), width=TEXT_W - 36 - 50)
        words = _stack([self.perm_title, self.perm_detail], spacing=3)
        row = _stack([self.perm_icon, words], vertical=False, spacing=16)
        self.perm_button = NSButton.buttonWithTitle_target_action_("Open System Settings", self, "grant:")
        box = _fill_box(_fixed(_box(radius=12, fill=NSColor.controlBackgroundColor(), border=NSColor.separatorColor()),
                               width=TEXT_W), _stack([row, self.perm_button], spacing=16), inset=18)
        self.perm_note = _text("Without it, Qualm sees only app names, and can't tell a lecture from a feed.",
                               12, 0.0, NSColor.secondaryLabelColor(), width=TEXT_W)
        # Screen Recording: optional, and said plainly what it's for.
        self.rec_icon = _symbol("rectangle.dashed.badge.record", 22, NSColor.secondaryLabelColor(), 0.2)
        _fixed(self.rec_icon, 34, 34)
        self.rec_title = _text("Screen Recording is off (optional)", 13, 0.3)
        self.rec_ask = (f"In System Settings › Privacy & Security › {screen_pane()}, turn on {who}, then "
                        "reopen Qualm if macOS asks.")
        self.rec_detail = _text("", 12, 0.0, NSColor.secondaryLabelColor(), width=TEXT_W - 36 - 50)
        rec_row = _stack([self.rec_icon, _stack([self.rec_title, self.rec_detail], spacing=3)], vertical=False,
                         spacing=16)
        rec_row.setAlignment_(NSLayoutAttributeTop)
        self.rec_button = NSButton.buttonWithTitle_target_action_("Allow Screen Recording", self, "grantScreen:")
        rec_box = _fill_box(_fixed(_box(radius=12, fill=NSColor.controlBackgroundColor(),
                                        border=NSColor.separatorColor()), width=TEXT_W),
                            _stack([rec_row, self.rec_button], spacing=12), inset=18)
        return self._page(2, "Let Qualm see the front window",
                          "Qualm reads the front window's title, address and a few headings through macOS "
                          "Accessibility.", box, self.perm_note, rec_box, spacing=16)

    @objc.python_method
    def _rules(self):
        rows, self.rule_switches = [], []
        starters = s.starters()
        for i, x in enumerate(starters):
            title, symbol = s.RULE_LOOK.get(x["id"], (x["what"].split(",")[0].capitalize(), "circle"))
            what = x["what"]
            examples = what.split("such as ", 1)[1] if "such as " in what else what.split(": ", 1)[-1]
            words = _stack([_text(title, 13, 0.3),
                            _text(examples[:1].upper() + examples[1:], 11.5, 0.0, NSColor.secondaryLabelColor(),
                                  width=TEXT_W - 28 - 30 - 14 - 140)], spacing=1)
            tag = _text("Asks first" if x["kind"] != "steps in" else "Steps in", 11.5, 0.0,
                        NSColor.secondaryLabelColor())
            switch = NSSwitch.alloc().init()
            switch.setState_(1 if x["on"] else 0)
            self.rule_switches.append((x["id"], switch))
            row = _stack([_tile(symbol, 30), words], vertical=False, spacing=14)
            row.addView_inGravity_(tag, NSStackViewGravityTrailing)
            row.addView_inGravity_(switch, NSStackViewGravityTrailing)
            _fixed(row, width=TEXT_W - 28)
            rows.append(row)
            if i < len(starters) - 1:
                rows.append(_separator(TEXT_W - 28))
        lst = _stack(rows, spacing=10)
        box = _fill_box(_fixed(_box(radius=12, fill=NSColor.controlBackgroundColor(), border=NSColor.separatorColor()),
                               width=TEXT_W), lst, inset=14)
        return self._page(3, "What should Qualm watch for?",
                          "Turn off what you don't need. You can change these any time from the menu bar.", box)

    @objc.python_method
    def _ready(self):
        self.summary = _stack([], spacing=10)
        self.login = NSButton.checkboxWithTitle_target_action_("Open Qualm when I log in", None, None)
        # From a checkout it starts unticked, as `qualm setup` asks it: a login item there runs Python,
        # which has no Accessibility of its own (the menu offers "Open at login" from Qualm.app only).
        self.login.setState_(1 if paths.bundle() else 0)
        opts = [self.login]
        self.shim = None
        if paths.bundle():
            self.shim = NSButton.checkboxWithTitle_target_action_("Add the qualm command to Terminal", None, None)
            self.shim.setState_(1)
            opts.append(self.shim)
        box = _fill_box(_fixed(_box(radius=12, fill=NSColor.controlBackgroundColor(), border=NSColor.separatorColor()),
                               width=TEXT_W), self.summary, inset=18)
        self.status = _text("", 12, 0.0, NSColor.secondaryLabelColor(), width=TEXT_W)
        return self._page(4, "Ready when you are",
                          "Qualm lives in the menu bar: click its eye for focus sessions, pauses and your dashboard.",
                          box, _stack(opts, spacing=8), self.status, spacing=18)  # room for two lines of status

    @objc.python_method
    def _fill_summary(self):
        for v in list(self.summary.views()):
            self.summary.removeView_(v)
        on = [s.RULE_LOOK.get(i, (i, ""))[0] for i, sw in self.rule_switches if sw.state()]
        lines = [
            ("laptopcomputer" if self.backend == "kev" else "cloud",
             "The model runs on this Mac" if self.backend == "kev" else "The model is hosted by TypeSafe",
             (f"The first start downloads about {need:.0f} GB; the menu bar shows how it's going."
              if (need := localmodel.disk_needed_gb()) >= 0.5
              else "It's on this Mac already: ready in about half a minute.")
             if self.backend == "kev" else "Each new screen's text goes to TypeSafe to be judged."),
            ("checklist", f"Watching for {len(on)} of {len(self.rule_switches)} things" if on else "Watching for nothing yet",
             ", ".join(on[:1] + [t[:1].lower() + t[1:] for t in on[1:]]) if on
             else "Turn rules on from the menu bar when you're ready."),
            ("checkmark.shield" if s.accessibility() else "exclamationmark.triangle",
             "Accessibility is on" if s.accessibility() else "Accessibility is off",
             "Qualm can read the front window." if s.accessibility() else "Qualm will only see app names until you turn it on."),
            ("sparkles", "Change rules with your AI agent",
             "Claude Code, Codex or the like: the menu bar has a prompt for it. It tests each change on your screens first."),
        ]
        for sym, title, detail in lines:
            words = _stack([_text(title, 13, 0.3), _text(detail, 12, 0.0, NSColor.secondaryLabelColor(),
                                                            width=TEXT_W - 36 - 30 - 14)], spacing=1)
            row = _stack([_tile(sym, 30), words], vertical=False, spacing=14)
            row.setAlignment_(NSLayoutAttributeCenterY)
            self.summary.addArrangedSubview_(row)

    # -- the bar ------------------------------------------------------------------

    @objc.python_method
    def _bar(self, root):
        bar = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, BAR_H))
        root.addSubview_(bar)
        _pin(bar, root, bottom=0, leading=0, trailing=0)
        _fixed(bar, height=BAR_H)
        line = _separator(W)
        bar.addSubview_(line)
        _pin(line, bar, top=0, leading=0)
        self.back = NSButton.buttonWithTitle_target_action_("Back", self, "back:")
        self.next = NSButton.buttonWithTitle_target_action_("Continue", self, "next:")
        self.next.setKeyEquivalent_("\r")
        for b in (self.back, self.next):
            b.setControlSize_(3)  # large
            _fixed(b, width=120)
            bar.addSubview_(b)
        self.spinner = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(0, 0, 16, 16))
        self.spinner.setStyle_(1)  # spinning
        self.spinner.setControlSize_(1)
        self.spinner.setDisplayedWhenStopped_(False)
        _fixed(self.spinner, 16, 16)
        bar.addSubview_(self.spinner)
        self.dots = []
        for _ in self.pages:
            d = _fixed(_box(radius=3.5, fill=NSColor.quaternaryLabelColor()), 7, 7)
            self.dots.append(d)
        dots = _stack(self.dots, vertical=False, spacing=8)
        bar.addSubview_(dots)
        NSLayoutConstraint.activateConstraints_([
            self.back.leadingAnchor().constraintEqualToAnchor_constant_(bar.leadingAnchor(), SIDE),
            self.back.centerYAnchor().constraintEqualToAnchor_(bar.centerYAnchor()),
            self.next.trailingAnchor().constraintEqualToAnchor_constant_(bar.trailingAnchor(), -SIDE),
            self.next.centerYAnchor().constraintEqualToAnchor_(bar.centerYAnchor()),
            self.spinner.trailingAnchor().constraintEqualToAnchor_constant_(self.next.leadingAnchor(), -12),
            self.spinner.centerYAnchor().constraintEqualToAnchor_(bar.centerYAnchor()),
            dots.centerXAnchor().constraintEqualToAnchor_(bar.centerXAnchor()),
            dots.centerYAnchor().constraintEqualToAnchor_(bar.centerYAnchor()),
        ])
        return bar

    # -- behaviour ----------------------------------------------------------------

    @objc.python_method
    def _go(self, page):
        self.page = page
        for i, p in enumerate(self.pages):
            p.setHidden_(i != page)
        for i, d in enumerate(self.dots):
            d.setFillColor_(NSColor.controlAccentColor() if i == page else NSColor.quaternaryLabelColor())
        self.back.setHidden_(page == 0)
        self.next.setTitle_({0: "Get Started", len(self.pages) - 1: "Start Qualm"}.get(page, "Continue"))
        if page == 2:
            self._check_permission()
        if page == len(self.pages) - 1:
            self._fill_summary()
        if page == 1 and self.backend == "jev" and self.key is not None:
            self.window.makeFirstResponder_(self.key)
        # Opened from the disk image: said before Start Qualm refuses (login and the command need a real copy).
        self._say(MOVE_FIRST if page == len(self.pages) - 1 and autostart.temporary_bundle() else "")

    @objc.python_method
    def _choose(self, backend):
        self.backend = backend
        for card, b in ((self.card_local, "kev"), (self.card_hosted, "jev")):
            on = b == backend
            card.setBorderWidth_(2 if on else 1)
            card.setBorderColor_(NSColor.controlAccentColor() if on else NSColor.separatorColor())
            card.setFillColor_(NSColor.controlAccentColor().colorWithAlphaComponent_(0.07) if on
                               else NSColor.controlBackgroundColor())
            card.icon.setContentTintColor_(NSColor.controlAccentColor() if on else NSColor.secondaryLabelColor())
        self.key_row.setHidden_(backend != "jev")
        if backend == "jev" and self.key is not None and self.page == 1:
            self.window.makeFirstResponder_(self.key)

    @objc.python_method
    def _check_permission(self):
        ok = s.accessibility()
        self.perm_icon.setImage_(NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "checkmark.circle.fill" if ok else "lock.fill", None).imageWithSymbolConfiguration_(
            NSImageSymbolConfiguration.configurationWithPointSize_weight_(26, 0.2)))
        self.perm_icon.setContentTintColor_(NSColor.systemGreenColor() if ok else NSColor.secondaryLabelColor())
        self.perm_title.setStringValue_("Accessibility is on" if ok else "Accessibility is off")
        self.perm_detail.setStringValue_("Qualm can read the front window. It's in System Settings › Privacy & Security "
                                         "› Accessibility, if you ever want it off." if ok else self.perm_ask)
        self.perm_button.setHidden_(ok)
        self.perm_note.setHidden_(ok)
        rec = screen_recording()
        self.rec_icon.setContentTintColor_(NSColor.systemGreenColor() if rec else NSColor.secondaryLabelColor())
        self.rec_title.setStringValue_("Screen Recording is on" if rec else "Screen Recording is off (optional)")
        self.rec_detail.setStringValue_(
            "Qualm can read apps that draw their text as pixels, and keeps a small screenshot of each new screen "
            f"for your review page. It's in System Settings › Privacy & Security › {screen_pane()}." if rec else
            "Qualm reads a picture of windows that draw their text as pixels, and keeps a small screenshot of "
            f"each new screen for your review page. {self.rec_ask}")
        self.rec_button.setHidden_(rec)

    def tick_(self, timer):
        if self.page == 2:
            self._check_permission()

    def getKey_(self, sender):
        subprocess.run(["open", KEY_URL], check=False)

    def grant_(self, sender):
        s.ask_accessibility()

    def grantScreen_(self, sender):
        ask_screen_recording()

    def back_(self, sender):
        if self.page > 0 and self.next.isEnabled():
            self._go(self.page - 1)

    def next_(self, sender):
        if self.page == 1 and self.backend == "jev":
            key = self._key()
            if not key and not self.has_key:
                self._say("The hosted model needs a TypeSafe API key.", True)
                return
            if key:
                self._busy("Checking the key…")
                threading.Thread(target=self._check_key, args=(key,), daemon=True).start()
                return
        if self.page < len(self.pages) - 1:
            self._go(self.page + 1)
            return
        # The key only goes with the hosted model: it was checked on the model page then.
        choices = s.Choices(self.backend, [rid for rid, sw in self.rule_switches if sw.state()],
                            bool(self.login.state()), self._key() if self.backend == "jev" else None,
                            bool(self.shim.state()) if self.shim else False)
        self._busy("Setting up…")
        threading.Thread(target=self._apply, args=(choices,), daemon=True).start()

    @objc.python_method
    def _key(self):
        return (str(self.key.stringValue()).strip() or None) if self.key is not None else None

    @objc.python_method
    def _busy(self, text):
        self.next.setEnabled_(False)
        self.back.setEnabled_(False)
        self.spinner.startAnimation_(None)
        self._say(text)

    @objc.python_method
    def _idle(self, text="", error=False):
        self.next.setEnabled_(True)
        self.back.setEnabled_(True)
        self.spinner.stopAnimation_(None)
        self._say(text, error)

    @objc.python_method
    def _say(self, text, error=False):
        """A line of status or error under the page: the key on the model page, setting up on the last."""
        self.model_error.setStringValue_(text if self.page == 1 else "")
        self.model_error.setHidden_(not (text and self.page == 1))
        self.status.setStringValue_(text if self.page == len(self.pages) - 1 else "")
        self.status.setHidden_(not (text and self.page == len(self.pages) - 1))
        for label in (self.model_error, self.status):
            label.setTextColor_(NSColor.systemRedColor() if error else NSColor.secondaryLabelColor())

    @objc.python_method
    def _check_key(self, key):
        err = s.check_key(key)
        AppHelper.callAfter(self._key_checked, err)

    @objc.python_method
    def _key_checked(self, err):
        if err:
            self._idle(key_trouble(err), True)
            return
        self._idle()
        self._go(self.page + 1)

    @objc.python_method
    def _apply(self, choices):
        try:
            s.apply(choices)
        except Exception as e:
            text = MOVE_FIRST if str(e) == autostart.MOVE_FIRST else f"Couldn't finish: {e}"
            AppHelper.callAfter(self._idle, text, True)
            return
        AppHelper.callAfter(self._finish)

    @objc.python_method
    def _finish(self):
        self.timer.invalidate()
        self.window.orderOut_(None)
        self.on_done()

    def windowShouldClose_(self, sender):
        NSApp.terminate_(self)  # closed before it's done: nothing to watch with yet
        return True
