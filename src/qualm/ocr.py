"""Text from pixels, for windows whose text isn't in the Accessibility tree.

Video players, canvases and some Electron or game UIs draw their text as
pixels: the AX walk finds a title and nothing else. For those, capture()
takes a picture of just that window and reads it with Vision (on the Mac,
no network). It needs Screen Recording permission, the same as the review
screenshots; without it, or without a window to picture, it returns nothing
and the state stays as the AX walk left it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time

LANGUAGES = ("zh-Hans", "en-US")
MIN_LINE = 2  # "推荐" is two characters and says a lot
REUSE_S = 30.0  # the same window's picture is read again at most this often, like the watcher's recheck

_cache: dict[tuple, tuple[float, list[str]]] = {}


def window_id(pid: int, frame: list[float]) -> int | None:
    """The CoreGraphics id of `pid`'s on-screen window at `frame` (x, y, w, h in
    points, as AX reports it); else its frontmost normal window."""
    from Quartz import (CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListExcludeDesktopElements,
                        kCGWindowListOptionOnScreenOnly)

    windows = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
                                         kCGNullWindowID) or []
    mine = [w for w in windows if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer", 0) == 0]
    for w in mine:
        b = w.get("kCGWindowBounds") or {}
        box = [b.get("X", 0), b.get("Y", 0), b.get("Width", 0), b.get("Height", 0)]
        if frame and all(abs(a - c) <= 2 for a, c in zip(box, frame)):
            return int(w["kCGWindowNumber"])
    return int(mine[0]["kCGWindowNumber"]) if mine else None


def recognize(image, fast: bool = False) -> list[str]:
    """The lines of text in an image (a file path or a CGImage), top to bottom."""
    import Vision
    from Foundation import NSURL

    if isinstance(image, str):
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(image), None)
    else:
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(1 if fast else 0)  # 0 accurate (reads Chinese), 1 fast (Latin only)
    request.setRecognitionLanguages_(list(LANGUAGES))
    request.setUsesLanguageCorrection_(True)
    ok, _ = handler.performRequests_error_([request], None)
    if not ok:
        return []
    found = []
    for r in request.results() or []:
        cands = r.topCandidates_(1)
        if cands:
            box = r.boundingBox()  # normalized, origin bottom left
            found.append((-round(box.origin.y, 2), box.origin.x, str(cands[0].string())))
    return [text for _, _, text in sorted(found)]


def window_text(pid: int, frame: list[float], title: str = "") -> list[str]:
    """The text in `pid`'s window, read from a picture of it; [] if it can't be had."""
    key = (pid, title, tuple(round(v) for v in frame))
    now = time.monotonic()
    if key in _cache and now - _cache[key][0] < REUSE_S:
        return _cache[key][1]
    wid = window_id(pid, frame)
    if wid is None:
        return []
    image = _picture(wid)
    lines = recognize(image) if image is not None else _via_screencapture(wid)
    lines = [s for s in lines if len(s.strip()) >= MIN_LINE]
    _cache[key] = (now, lines)
    if len(_cache) > 64:
        _cache.pop(next(iter(_cache)))
    return lines


def _picture(wid: int):
    """The window as a CGImage, in this process (no screencapture to start); None where macOS refuses."""
    try:
        from Quartz import (CGRectNull, CGWindowListCreateImage, kCGWindowImageBoundsIgnoreFraming,
                            kCGWindowImageNominalResolution, kCGWindowListOptionIncludingWindow)

        return CGWindowListCreateImage(CGRectNull, kCGWindowListOptionIncludingWindow, wid,
                                       kCGWindowImageBoundsIgnoreFraming | kCGWindowImageNominalResolution)
    except Exception:
        return None


def _via_screencapture(wid: int) -> list[str]:
    fd, path = tempfile.mkstemp(prefix="qualm-ocr-", suffix=".png")
    os.close(fd)
    try:
        # -o: no shadow; -x: no sound. Needs Screen Recording; without it the file stays empty.
        r = subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), "-t", "png", path],
                           capture_output=True, timeout=3)
        return recognize(path) if r.returncode == 0 and os.path.getsize(path) > 0 else []
    except (OSError, subprocess.TimeoutExpired):
        return []
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
