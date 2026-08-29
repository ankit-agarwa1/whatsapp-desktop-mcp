"""Unit tests for ``sender.pasteboard`` — the image-send clipboard loader.

The pasteboard is the only route an image can reach WhatsApp Desktop: the
``whatsapp://send`` URL scheme carries text only. These tests pin the
guard rails that stop a bad input from reaching the UI-driving code with
the user's clipboard already clobbered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whatsapp_desktop_mcp.sender import pasteboard


def test_missing_file_raises_before_touching_the_pasteboard(tmp_path: Path) -> None:
    """A path that does not exist must fail without clobbering the clipboard."""
    with pytest.raises(FileNotFoundError, match="no image file at"):
        pasteboard.put_image_on_clipboard(tmp_path / "nope.png")


def test_unsupported_suffix_is_rejected(tmp_path: Path) -> None:
    """Suffix check runs before decode so the error names the real problem."""
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    with pytest.raises(ValueError, match="unsupported image type"):
        pasteboard.put_image_on_clipboard(p)


def test_image_suffix_but_undecodable_bytes_is_rejected(tmp_path: Path) -> None:
    """NSImage returns nil rather than raising — a truncated or mislabelled
    download must not silently put nothing on the clipboard and paste an
    empty compose box."""
    p = tmp_path / "broken.png"
    p.write_bytes(b"<!doctype html><html>not a png</html>")
    with pytest.raises(ValueError, match="could not be decoded as an image"):
        pasteboard.put_image_on_clipboard(p)


def test_unavailable_pyobjc_raises_structurally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-06: a broken pyobjc install degrades to a structured error, not a
    traceback, and never reaches the pasteboard call."""
    monkeypatch.setattr(pasteboard, "_PYOBJC_AVAILABLE", False)
    from whatsapp_desktop_mcp.exceptions import AccessibilityAPIUnavailable

    with pytest.raises(AccessibilityAPIUnavailable, match="pyobjc not available"):
        pasteboard.put_image_on_clipboard(tmp_path / "anything.png")
