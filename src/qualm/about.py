"""About Qualm: what it is, which version, who made it, and where to find more.

A small window in the setup window's style (system colors, so it follows
light and dark mode), opened from the menu bar. The website and the maker's
page open in the browser; Esc or Cmd-W closes it.
"""

from __future__ import annotations

import subprocess

import objc
from AppKit import (
    NSApp,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSCursor,
    NSEventModifierFlagCommand,
    NSEventModifierFlagDeviceIndependentFlagsMask,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageView,
    NSLayoutAttributeCenterX,
    NSMakeRect,
    NSPanel,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowMiniaturizeButton,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
    NSWindowZoomButton,
)
from Foundation import NSObject

from . import updates
from .onboard import ICON, _fixed, _pin, _stack, _text

WEBSITE = "https://qualm.r-q.name"
MAKER, MAKER_PAGE = "Tianrun Qiu", "https://r-q.name"
TITLE = "About Qualm"
W = 360.0  # content width
SIDE = 36.0


def version_line() -> str:
    """"Version 0.1.0 beta 1 (52)" from Qualm.app, as Mac apps say it; a checkout says it runs from source."""
    v = updates.pretty(updates.version())
    return f"Version {v} ({updates.build()})" if updates.build() else f"Version {v}, from source"


class AboutPanel(NSPanel):
    """Closes on Cmd-W as well as Esc: a menu bar app has no menu to carry the shortcut."""

    def performKeyEquivalent_(self, event):
        flags = event.modifierFlags() & NSEventModifierFlagDeviceIndependentFlagsMask
        if flags == NSEventModifierFlagCommand and event.charactersIgnoringModifiers() == "w":
            self.performClose_(None)
            return True
        return objc.super(AboutPanel, self).performKeyEquivalent_(event)


class LinkButton(NSButton):
    """Text that opens a page: the link color, and the pointing hand over it."""

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), NSCursor.pointingHandCursor())


class About(NSObject):
    @objc.python_method
    def setup(self):
        self.urls: list[str] = []
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskFullSizeContentView
        self.window = AboutPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, 300), style, NSBackingStoreBuffered, False)
        self.window.setTitle_(TITLE)
        self.window.setTitleVisibility_(NSWindowTitleHidden)
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setMovableByWindowBackground_(True)
        self.window.setHidesOnDeactivate_(False)  # stays when the browser opens the website
        self.window.setReleasedWhenClosed_(False)
        for b in (NSWindowMiniaturizeButton, NSWindowZoomButton):  # only the close button, as About windows have
            self.window.standardWindowButton_(b).setHidden_(True)

        icon = NSImageView.imageViewWithImage_(NSImage.alloc().initWithContentsOfFile_(str(ICON)))
        _fixed(icon, 88, 88)
        text_w = W - 2 * SIDE
        made = _stack([_text("Made by", 13, 0.0, NSColor.secondaryLabelColor()),
                       self._link(MAKER, MAKER_PAGE, 13, 0.3)], vertical=False, spacing=0)
        self.website = NSButton.buttonWithTitle_target_action_("Visit qualm.r-q.name", self, "openLink:")
        self.website.setTag_(self._url(WEBSITE))
        self.website.setToolTip_(WEBSITE)
        self.website.setKeyEquivalent_("\r")
        self.website.setControlSize_(3)  # large, as the setup window's buttons
        col = _stack([
            icon,
            _text("Qualm", 22, 0.4),
            _text(version_line(), 12, 0.0, NSColor.secondaryLabelColor()),
            _text("A second thought before the scroll.", 13, 0.0, NSColor.secondaryLabelColor(), width=text_w,
                  center=True),
            made,
            _text("Built on Kev and TypeSafe's Jev.\nFree software under the GNU GPL v3 or later.", 11, 0.0,
                  NSColor.tertiaryLabelColor(), width=text_w, center=True),
            self.website,
        ], spacing=4, align=NSLayoutAttributeCenterX)
        col.setCustomSpacing_afterView_(12, icon)
        col.setCustomSpacing_afterView_(10, col.views()[2])  # the version
        col.setCustomSpacing_afterView_(14, col.views()[3])  # the line under it
        col.setCustomSpacing_afterView_(6, made)
        col.setCustomSpacing_afterView_(20, col.views()[5])
        root = self.window.contentView()
        root.addSubview_(col)
        _pin(col, root, top=40, bottom=24)
        col.centerXAnchor().constraintEqualToAnchor_(root.centerXAnchor()).setActive_(True)
        _fixed(root, width=W)
        root.layoutSubtreeIfNeeded()
        self.window.setContentSize_(root.fittingSize())
        return self

    @objc.python_method
    def _url(self, url: str) -> int:
        self.urls.append(url)
        return len(self.urls) - 1

    @objc.python_method
    def _link(self, title: str, url: str, size: float, weight: float) -> NSButton:
        b = LinkButton.buttonWithTitle_target_action_(title, self, "openLink:")
        b.setBordered_(False)
        b.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, {
            NSFontAttributeName: NSFont.systemFontOfSize_weight_(size, weight),
            NSForegroundColorAttributeName: NSColor.linkColor()}))
        b.setTag_(self._url(url))
        b.setToolTip_(url)
        b.sizeToFit()
        return b

    @objc.python_method
    def show(self):
        if not self.window.isVisible():
            self.window.center()
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def openLink_(self, sender):
        subprocess.run(["open", self.urls[sender.tag()]], check=False)
