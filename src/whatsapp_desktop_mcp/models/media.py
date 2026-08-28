"""MediaRef — attachment metadata only; never inline bytes (DATA-03, DATA-04).

CLAUDE.md hard rule #4: "Never inline media bytes in tool responses.
Surface attachments as ``MediaRef { filename, mime, local_path,
size_bytes }``." A 4 MB image is roughly 1.5 M tokens after base64; the
MCP 25k-token output cap would be blown by a single attachment.

DATA-03 mandates this surface: ``filename`` + ``mime`` + absolute
``local_path`` + ``size_bytes``. Plan 02's ``reader/media.py`` resolves
the ``ZWAMEDIAITEM.ZMEDIALOCALPATH`` column against
``paths.resolve_media_root()`` (Plan 01-01 Task 3) and applies a
``Path.resolve()`` + prefix-check guardrail before populating
``local_path`` — the actual path-traversal defense (T-01-04) lives there.

DATA-04 forbids parsing ``ZWAMEDIAITEM.ZMEDIAKEY`` and
``ZWAMEDIAITEM.ZMETADATA`` (encrypted / protobuf BLOBs) and
``ZWAMESSAGEINFO.ZRECEIPTINFO`` (also encrypted). This model exposes
NONE of those fields — only the metadata columns
(``ZMEDIALOCALPATH``, ``ZFILESIZE``, ``ZMOVIEDURATION``, ``ZTITLE``)
verified live on the user's Mac.

``ZWAMEDIAITEM.ZLATITUDE`` / ``ZLONGITUDE`` are deliberately NOT
surfaced: on a file-backed media row they hold the image/video pixel
dimensions, not coordinates (verified live — type 1 "latitude" ranges
16.0..4160.0). Real coordinates only exist on ``ZMESSAGETYPE = 5``
rows, which carry no ``ZMEDIALOCALPATH`` and therefore never produce a
``MediaRef`` at all.
"""

from __future__ import annotations

from pydantic import BaseModel


class MediaRef(BaseModel):
    """Reference to an attachment file on disk (DATA-03 / DATA-04 surface)."""

    local_path: str
    filename: str
    mime: str
    size_bytes: int
    duration_seconds: float | None = None
    caption: str | None = None
