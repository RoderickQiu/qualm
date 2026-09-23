"""What capture() keeps of a window title."""

from qualm.state import _title


def test_browser_additions_are_dropped_from_titles():
    assert _title("(1) Notifications | LinkedIn - High memory usage - 1.4 GB - Google Chrome", "Google Chrome") \
        == "Notifications | LinkedIn"
    assert _title("Make Something | Y Combinator - Google Chrome (Incognito)", "Google Chrome") == "Make Something | Y Combinator"
    assert _title("Page - Google Chrome - Work", "Google Chrome") == "Page"


def test_other_titles_are_kept():
    assert _title("state.py — qualm", "Cursor") == "state.py — qualm"
    assert _title("Google Chrome", "Google Chrome") == "Google Chrome"
    assert _title("Qualm review", "Google Chrome") == "Qualm review"
