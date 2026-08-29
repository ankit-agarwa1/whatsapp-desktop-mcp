"""macOS pasteboard image loading for the image-send path.

The ``whatsapp://send`` URL scheme carries **text only**, so an image
cannot be delivered the way a body is. The only route through WhatsApp
Desktop is the one a human uses: put the image on the system pasteboard,
paste it into the focused chat's compose box, and send.

**Why pyobjc and not ``osascript``:** AppleScript's
``set the clipboard to (read ... as «class PNGf»)`` requires the caller to
name the image's file type up front and silently produces garbage when the
guess is wrong. ``NSImage`` sniffs the format itself, so one code path
covers PNG, JPEG, HEIC, GIF and TIFF.

**Why D-06 try/except ImportError:** identical reasoning to
``sender.ax_assert`` — a broken pyobjc install must not take the whole MCP
server down at import time. The read tools keep working and only the image
path raises :class:`AccessibilityAPIUnavailable`.

**The pasteboard is global, shared, user-visible state.** Writing to it
clobbers whatever the user had copied. That is unavoidable on this route
and is called out in the tool's docstring so the cost is visible at the
call site rather than buried here.
"""

from __future__ import annotations

import logging
from pathlib import Path

from whatsapp_desktop_mcp.exceptions import AccessibilityAPIUnavailable

logger = logging.getLogger(__name__)

try:
    from AppKit import NSImage, NSPasteboard  # type: ignore[import-untyped]

    _PYOBJC_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on broken installs
    _PYOBJC_AVAILABLE = False

# Formats NSImage decodes natively. Checked before touching the pasteboard
# so an unsupported file fails with a clear message instead of silently
# putting nothing on the clipboard and pasting an empty compose box.
SUPPORTED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".heic", ".tiff", ".tif", ".webp"})


def put_image_on_clipboard(path: str | Path) -> Path:
    """Load ``path`` and place it on the general pasteboard as an image.

    Returns the resolved path on success.

    Raises:
        AccessibilityAPIUnavailable: pyobjc missing or broken.
        FileNotFoundError: no file at ``path``.
        ValueError: suffix not in :data:`SUPPORTED_SUFFIXES`, or the file
            is not decodable as an image (a ``.png`` that is actually HTML,
            a truncated download).
    """
    if not _PYOBJC_AVAILABLE:
        raise AccessibilityAPIUnavailable(
            "pyobjc not available; cannot place an image on the pasteboard — "
            "reinstall pyobjc-core and pyobjc-framework-Cocoa to enable image sends"
        )

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"no image file at {resolved}")

    suffix = resolved.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"unsupported image type {suffix!r} for {resolved}; "
            f"supported: {sorted(SUPPORTED_SUFFIXES)}"
        )

    image = NSImage.alloc().initWithContentsOfFile_(str(resolved))
    # NSImage returns nil rather than raising when the bytes are not a
    # decodable image, so this branch catches a mislabelled or truncated
    # file that passed the suffix check.
    if image is None:
        raise ValueError(f"{resolved} has an image suffix but could not be decoded as an image")

    pasteboard = NSPasteboard.generalPasteboard()
    pasteboard.clearContents()
    if not pasteboard.writeObjects_([image]):
        raise ValueError(f"pasteboard rejected the image at {resolved}")

    logger.debug("put_image_on_clipboard: wrote %s to the general pasteboard", resolved)
    return resolved
