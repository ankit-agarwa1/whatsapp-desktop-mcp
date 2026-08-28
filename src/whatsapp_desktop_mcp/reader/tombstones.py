"""Tombstone predicate for deleted messages (READ-08, P10 mitigation).

A row is tombstoned iff ``ZMESSAGETYPE == 14`` (deleted-for-everyone).
Verified live on the user's Mac: 756 type-14 rows, all bodyless, all
carrying the revoked stanza id in ``ZWAMEDIAITEM.ZTITLE``.

**Do not re-add a ``ZFLAGS`` clause.** v0.1 also treated
``ZTEXT IS NULL AND (ZFLAGS & 0xFF000000) = 0x05000000`` as a deletion
marker. That bit pattern is the *normal* high byte of a message with an
attachment, and image/video/sticker/document rows never carry ``ZTEXT``
(their caption lives in ``ZWAMEDIAITEM.ZTITLE``), so the clause matched
"uncaptioned media" and silently dropped 6629 of the 7273 rows that have
a file on disk — 91% of every attachment in the corpus.
"""

from __future__ import annotations

# SQL fragment for inlining into reader SQL templates. Single source of
# truth: ``reader/schema_v1.py`` templates reference this constant.
TOMBSTONE_SQL_WHERE: str = "ZMESSAGETYPE != 14"


def is_tombstone(message_type: int) -> bool:
    """Return True if the row is a deleted-for-everyone (revoked) message.

    Default ``include_deleted=False`` filters at SQL level via
    :data:`TOMBSTONE_SQL_WHERE` (uses indexes); this Python predicate
    exists as a row-level fallback for callers that already have row
    data in hand.
    """
    return message_type == 14
