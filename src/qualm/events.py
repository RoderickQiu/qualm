"""Wake the watcher when the front window changes, instead of reading it every 0.5 s.

Reading the front window walks its Accessibility tree, which cost the app
~2% CPU when done twice a second whatever happened (measured 2026-09-23).
Now macOS says when something changes: another app comes to the front, its
focused or main window changes, or a title changes (a new page, a new tab,
the next video). The watcher still looks every few seconds on its own
(IDLE_S), for text that changes in place: a feed scrolled, a chat.

The observers live on the main run loop, so this runs only inside the app
(`qualm watch` keeps plain polling).
"""

from __future__ import annotations

import threading

import objc
from ApplicationServices import (
    AXObserverAddNotification,
    AXObserverCreate,
    AXObserverGetRunLoopSource,
    AXUIElementCreateApplication,
    kAXFocusedWindowChangedNotification,
    kAXMainWindowChangedNotification,
    kAXTitleChangedNotification,
)
from AppKit import NSWorkspace, NSWorkspaceDidActivateApplicationNotification
from CoreFoundation import CFRunLoopAddSource, CFRunLoopGetMain, CFRunLoopRemoveSource, kCFRunLoopDefaultMode

IDLE_S = 3.0  # with no event, the watcher still looks this often
NOTIFICATIONS = (kAXFocusedWindowChangedNotification, kAXMainWindowChangedNotification, kAXTitleChangedNotification)
_WAKES: list[threading.Event] = []  # PyObjC takes only a plain function as the AX callback


@objc.callbackFor(AXObserverCreate)
def _callback(observer, element, notification, refcon) -> None:
    for wake in _WAKES:
        wake.set()


class FrontWindowEvents:
    """Sets `wake` whenever the front app or its window changes. Start on the main thread."""

    def __init__(self, wake: threading.Event):
        self.wake = wake
        _WAKES.append(wake)
        self._observer = self._source = None
        self._pid = None
        self._token = None

    def start(self) -> "FrontWindowEvents":
        ws = NSWorkspace.sharedWorkspace()
        self._token = ws.notificationCenter().addObserverForName_object_queue_usingBlock_(
            NSWorkspaceDidActivateApplicationNotification, None, None, self._activated)
        front = ws.frontmostApplication()
        if front is not None:
            self._observe(front.processIdentifier())
        return self

    def _activated(self, note) -> None:
        app = note.userInfo().get("NSWorkspaceApplicationKey")
        if app is not None:
            self._observe(app.processIdentifier())
        self.wake.set()

    def _observe(self, pid: int) -> None:
        """Title and window changes of this app; the previous app's observer goes."""
        if pid == self._pid:
            return
        self._drop()
        err, observer = AXObserverCreate(pid, _callback, None)
        if err != 0 or observer is None:
            return  # no Accessibility yet, or an app that won't be observed: polling still covers it
        app = AXUIElementCreateApplication(pid)
        for name in NOTIFICATIONS:
            AXObserverAddNotification(observer, app, name, None)
        source = AXObserverGetRunLoopSource(observer)
        CFRunLoopAddSource(CFRunLoopGetMain(), source, kCFRunLoopDefaultMode)
        self._observer, self._source, self._pid = observer, source, pid

    def _drop(self) -> None:
        if self._source is not None:
            CFRunLoopRemoveSource(CFRunLoopGetMain(), self._source, kCFRunLoopDefaultMode)
        self._observer = self._source = self._pid = None
