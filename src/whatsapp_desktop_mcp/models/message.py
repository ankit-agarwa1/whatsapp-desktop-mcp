"""Message — the locked DATA-02 surface for ``ZWAMESSAGE`` rows.

A ``Message`` represents one row of ``ZWAMESSAGE`` after JID parsing,
Cocoa->Unix timestamp conversion, ``ZMESSAGETYPE`` -> ``MessageKind``
mapping, ``ZSTANZAID`` -> ``message_id`` projection, and (when present)
``ZWAMEDIAITEM`` -> :class:`MediaRef` resolution. Plan 02's reader
populates this; Plan 04's tools surface it.

``MessageKind`` mapping — see ``reader/messages._MESSAGE_TYPE_MAP`` for
the ``ZMESSAGETYPE`` table it is derived from. Any type not in that
table maps to ``"unknown"``; the reader never raises on a novel integer
and never guesses a specific kind for one.

B2 lock (do NOT add a public ``z_sort`` field): ``ZSORT`` is reader
internal — ``reader.window`` returns ``(Message, z_sort)`` tuples; the
cursor codec carries the float separately. Surfacing ``z_sort`` on the
public ``Message`` would invite callers to filter / sort on it, breaking
the opaque-cursor contract.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from whatsapp_desktop_mcp.models.contact import Jid
from whatsapp_desktop_mcp.models.media import MediaRef

MessageKind = Literal[
    "text",
    "image",
    "video",
    "audio",
    "document",
    "sticker",
    "contact",
    "location",
    "system",
    "revoked",
    "unknown",
]


class Message(BaseModel):
    """One ``ZWAMESSAGE`` row, normalized for tool output (DATA-02)."""

    message_id: str
    chat_id: int
    sender_jid: Jid
    timestamp: int
    body: str | None
    kind: MessageKind
    is_outgoing: bool
    is_starred: bool
    quoted_message_id: str | None
    media: MediaRef | None
