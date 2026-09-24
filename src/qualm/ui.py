"""The look: a dark, blurred panel, like the system's own HUDs, and a soft
dim over the screens behind it. Frames are laid out by hand, top down.

Kept apart from app.py so the controller reads as behaviour, not pixels.
"""

from __future__ import annotations

import math
import time

import objc
from AppKit import (
    NSAnimationContext,
    NSAppearance,
    NSAppearanceNameVibrantDark,
    NSBackingStoreBuffered,
    NSBox,
    NSButton,
    NSColor,
    NSEventTypeKeyDown,
    NSFont,
    NSImage,
    NSImageSymbolConfiguration,
    NSImageView,
    NSLineBreakByTruncatingTail,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSTextField,
    NSVisualEffectView,
    NSWindow,
    NSWindowCloseButton,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowMiniaturizeButton,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
    NSWindowZoomButton,
    NSWorkspace,
)

PANEL_LEVEL = NSStatusWindowLevel + 1  # over the dim, which sits at the status level
MATERIAL_HUD = 13  # NSVisualEffectMaterialHUDWindow
BOX_CUSTOM = 4  # NSBoxCustom
DIM_ALPHA = 0.32

# Accents per kind of moment, as (r, g, b): calm, not alarming.
ACCENTS = {
    "deny": (1.0, 0.62, 0.35),  # warm amber
    "checkin": (0.70, 0.62, 1.0),  # soft violet: a question, not a stop
    "timesup": (0.98, 0.80, 0.33),  # soft yellow
    "focus": (0.38, 0.78, 0.98),  # clear blue
    "done": (0.45, 0.85, 0.55),  # green, for "got it"
    "watch": (0.38, 0.78, 0.98),  # the setup window: the menu bar's eye
    "agent": (0.62, 0.72, 1.0),  # periwinkle: hand it to your AI agent
    "warn": (1.0, 0.72, 0.30),  # amber: something stops Qualm from working
    "key": (0.38, 0.78, 0.98),  # the hosted model's key: the menu bar's blue
}
SYMBOLS = {"deny": "hand.raised.fill", "checkin": "timer", "timesup": "hourglass", "focus": "scope", "done": "checkmark",
           "watch": "eye", "agent": "sparkles", "warn": "exclamationmark.triangle.fill", "key": "key.fill"}


def rgb(r: float, g: float, b: float, a: float = 1.0) -> NSColor:
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


class QualmPanel(NSPanel):
    """The HUD's window. For a moment after `hush`, it ignores keys: a pop-up
    takes the keyboard while you may be typing on the page, and the keystroke
    already on its way (Return in a chat) mustn't answer it."""

    @objc.python_method
    def hush(self, seconds: float) -> None:
        self._quiet_until = time.monotonic() + seconds

    @objc.python_method
    def hushed(self) -> bool:
        return time.monotonic() < getattr(self, "_quiet_until", 0.0)

    def sendEvent_(self, event):
        if event.type() == NSEventTypeKeyDown and self.hushed():
            return
        objc.super(QualmPanel, self).sendEvent_(event)

    def performKeyEquivalent_(self, event):
        if self.hushed():
            return True  # swallowed: Return mustn't press the default button yet
        return objc.super(QualmPanel, self).performKeyEquivalent_(event)


def hud(width: float, height: float, title: str) -> tuple[NSPanel, NSVisualEffectView]:
    """A floating, blurred panel that takes keyboard input, on every Space and over full-screen apps."""
    panel = QualmPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, width, height), NSWindowStyleMaskTitled | NSWindowStyleMaskFullSizeContentView,
        NSBackingStoreBuffered, False)
    panel.setTitle_(title)  # never shown, but the watcher skips windows with this title
    panel.setTitleVisibility_(NSWindowTitleHidden)
    panel.setTitlebarAppearsTransparent_(True)
    for b in (NSWindowCloseButton, NSWindowMiniaturizeButton, NSWindowZoomButton):
        if (button := panel.standardWindowButton_(b)) is not None:
            button.setHidden_(True)
    panel.setMovableByWindowBackground_(True)
    panel.setLevel_(PANEL_LEVEL)
    panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary)
    panel.setHidesOnDeactivate_(False)
    panel.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameVibrantDark))
    fx = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    fx.setMaterial_(MATERIAL_HUD)
    fx.setBlendingMode_(0)  # behind window
    fx.setState_(1)  # always active, even when the panel isn't key
    panel.setContentView_(fx)
    return panel, fx


def label(text: str = "", size: float = 13, weight: float = 0.0, color: NSColor | None = None,
          width: float = 0, wrap: bool = False) -> NSTextField:
    f = NSTextField.wrappingLabelWithString_(text) if wrap else NSTextField.labelWithString_(text)
    f.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    f.setTextColor_(color or NSColor.labelColor())
    f.setSelectable_(False)
    if wrap:
        f.setPreferredMaxLayoutWidth_(width)
    else:
        f.setLineBreakMode_(NSLineBreakByTruncatingTail)
    if width:
        f.setFrameSize_((width, f.fittingSize().height))
    return f


def fit(field: NSTextField, text: str, width: float) -> float:
    """Set a wrapping label's text; returns the height it needs at `width`.
    Measured as the cell wraps it in a frame that wide: fittingSize alone
    can lay it out a hair wider and come up a line short."""
    field.setStringValue_(text)
    field.setPreferredMaxLayoutWidth_(width)
    h = 0.0
    if text:
        wrapped = field.cell().cellSizeForBounds_(NSMakeRect(0, 0, width, 10000)).height
        h = math.ceil(max(field.fittingSize().height, wrapped))
    field.setFrameSize_((width, h))
    field.setHidden_(not text)
    return h


def badge(kind: str, size: float = 46) -> tuple[NSBox, NSImageView]:
    """A soft round tile with an SF Symbol: the first thing the eye lands on."""
    r, g, b = ACCENTS[kind]
    box = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, size, size))
    box.setBoxType_(BOX_CUSTOM)
    box.setBorderWidth_(0)
    box.setCornerRadius_(size * 0.3)
    box.setFillColor_(rgb(r, g, b, 0.18))
    box.setTitle_("")
    view = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, size, size))
    set_symbol(view, kind, size)
    return box, view


def set_symbol(view: NSImageView, kind: str, size: float = 46) -> None:
    r, g, b = ACCENTS[kind]
    cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size * 0.42, 0.3)
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(SYMBOLS[kind], kind)
    view.setImage_(img.imageWithSymbolConfiguration_(cfg))
    view.setContentTintColor_(rgb(r, g, b))


def well(frame) -> NSBox:
    """A faint rounded inset, for text to copy."""
    box = NSBox.alloc().initWithFrame_(frame)
    box.setBoxType_(BOX_CUSTOM)
    box.setBorderWidth_(1)
    box.setBorderColor_(rgb(1, 1, 1, 0.10))
    box.setCornerRadius_(10)
    box.setFillColor_(rgb(1, 1, 1, 0.05))
    box.setTitle_("")
    return box


def recolor(box: NSBox, kind: str) -> None:
    r, g, b = ACCENTS[kind]
    box.setFillColor_(rgb(r, g, b, 0.18))


def link(title: str, target, action: str) -> NSButton:
    """A quiet, borderless text button, for the answers that aren't the point."""
    b = NSButton.buttonWithTitle_target_action_(title, target, action)
    b.setBordered_(False)
    b.setFont_(NSFont.systemFontOfSize_weight_(12, 0.0))
    b.setContentTintColor_(NSColor.secondaryLabelColor())
    b.sizeToFit()
    return b


def app_icon(bundle_id: str, size: float = 16) -> NSImage | None:
    ws = NSWorkspace.sharedWorkspace()
    url = ws.URLForApplicationWithBundleIdentifier_(bundle_id) if bundle_id else None
    if url is None:
        return None
    icon = ws.iconForFile_(url.path())
    icon.setSize_((size, size))
    return icon


def fade(window, alpha: float, seconds: float = 0.18, then=None) -> None:
    def change(ctx):
        ctx.setDuration_(seconds)
        window.animator().setAlphaValue_(alpha)

    NSAnimationContext.runAnimationGroup_completionHandler_(change, then)


class Dimmer:
    """A soft dark veil over every screen while the panel is up. With
    `block` it takes the clicks, so the pop-up is answered before you go
    on (the panel sits above it); without, it's a veil you click through."""

    def __init__(self):
        self.windows = []

    def show(self, block: bool = False) -> None:
        self.hide(animate=False)
        for screen in NSScreen.screens():
            w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                screen.frame(), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
            w.setBackgroundColor_(NSColor.blackColor())
            w.setOpaque_(False)
            w.setAlphaValue_(0.0)
            w.setIgnoresMouseEvents_(not block)
            w.setLevel_(NSStatusWindowLevel)
            w.setReleasedWhenClosed_(False)
            w.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | NSWindowCollectionBehaviorFullScreenAuxiliary
                                     | NSWindowCollectionBehaviorStationary)
            w.orderFrontRegardless()
            fade(w, DIM_ALPHA, 0.35)
            self.windows.append(w)

    def hide(self, animate: bool = True) -> None:
        windows, self.windows = self.windows, []
        for w in windows:
            if animate:
                fade(w, 0.0, 0.25, lambda w=w: w.orderOut_(None))
            else:
                w.orderOut_(None)
