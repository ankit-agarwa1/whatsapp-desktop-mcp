"""``is_tombstone`` + :data:`TOMBSTONE_SQL_WHERE` — READ-08 / P10.

Predicate: ``ZMESSAGETYPE == 14`` (deleted-for-everyone), and nothing
else. The Python predicate and the SQL constant are asserted together
so a future schema bump that changes one catches the other.
"""

from __future__ import annotations

from whatsapp_desktop_mcp.reader.tombstones import TOMBSTONE_SQL_WHERE, is_tombstone


def test_type_14_is_always_tombstone() -> None:
    """``ZMESSAGETYPE = 14`` is deleted-for-everyone regardless of body."""
    assert is_tombstone(14) is True


def test_normal_types_are_not_tombstones() -> None:
    """Text and every media type survive the filter."""
    for message_type in (0, 1, 2, 3, 8, 15, 20):
        assert is_tombstone(message_type) is False


def test_TOMBSTONE_SQL_WHERE_constant_present() -> None:
    """The SQL fragment matches the Python predicate exactly.

    It must NOT re-acquire a ZFLAGS clause: ``(ZFLAGS & 0xFF000000) =
    0x05000000`` is the normal high byte of a message with an
    attachment, and uncaptioned media has a NULL ZTEXT, so the v0.1
    ``ZTEXT IS NULL AND flags == 0x05000000`` clause dropped 91% of
    every attachment in the corpus.
    """
    assert TOMBSTONE_SQL_WHERE == "ZMESSAGETYPE != 14"
