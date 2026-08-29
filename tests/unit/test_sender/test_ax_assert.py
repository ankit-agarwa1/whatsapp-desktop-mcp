"""Unit tests for ``sender.ax_assert`` — load-bearing D-03 / SEND-04 P5 mitigation.

Covers:

* ``_strip_bidi`` — removes the three known bidi invisibles (U+200E LRM,
  U+2068 FSI, U+2069 PDI) WhatsApp Catalyst injects in AX labels.
* ``assert_focused_chat_matches`` — the load-bearing preflight:
  - pyobjc unavailable → :class:`AccessibilityAPIUnavailable`.
  - WhatsApp not running → :class:`ChatHeaderMismatch`.
  - focused-window AX lookup failure → :class:`ChatHeaderMismatch`.
  - matching heading (after bidi-strip + casefold) → returns None.
  - non-matching heading → :class:`ChatHeaderMismatch`.
* AX walk DoS guard (``_MAX_WALK_NODES = 200``).
* Case-fold substring match (locale variation tolerance).

The verified-live regression string ``"‎⁨Olivier Giffard⁩"`` (with U+200E
LRM, U+2068 FSI, U+2069 PDI invisibles) MUST strip cleanly to
``"Olivier Giffard"`` — that's the Pattern 2 / SP-3 verified-live
regression.
"""

from __future__ import annotations

import pytest
from ApplicationServices import (
    kAXChildrenAttribute,
    kAXDescriptionAttribute,
    kAXFocusedUIElementAttribute,
    kAXFocusedWindowAttribute,
    kAXIdentifierAttribute,
    kAXRoleAttribute,
)

from whatsapp_desktop_mcp.exceptions import (
    AccessibilityAPIUnavailable,
    ChatHeaderMismatch,
    ComposerNotFocused,
)
from whatsapp_desktop_mcp.sender import ax_assert

# ---------------------------------------------------------------------------
# _strip_bidi — pure helper, no AX-API dependency
# ---------------------------------------------------------------------------


def test_strip_bidi_removes_lrm_fsi_pdi() -> None:
    """VERIFIED-LIVE regression: the user's chat name with bidi invisibles strips cleanly.

    Per Pattern 2 verified-live on WhatsApp 26.16.74 (2026-05-13), the
    AX heading description for a contact named "Olivier Giffard" arrives
    as ``"\\u200E\\u2068Olivier Giffard\\u2069"``. After
    :func:`_strip_bidi` the three invisibles are gone and the result
    is the plain name.
    """
    # The verified-live observed string. Literal codepoints declared as
    # escape sequences to keep the source grep-stable (raw chars render
    # as zero-width invisibles).
    observed = "‎⁨Olivier Giffard⁩"
    assert ax_assert._strip_bidi(observed) == "Olivier Giffard"


def test_strip_bidi_preserves_normal_text() -> None:
    """Text with no bidi invisibles passes through unchanged."""
    assert ax_assert._strip_bidi("Plain text") == "Plain text"


def test_strip_bidi_strips_only_three_known_invisibles() -> None:
    """Other Unicode invisibles (e.g. U+200B ZWSP) are NOT stripped.

    The three bidi codepoints stripped are exactly U+200E / U+2068 / U+2069
    per the verified-live observation. Stripping a wider set (e.g. all
    Cf-category characters) would be a scope expansion that may produce
    surprising matches; keep the strip set narrow.
    """
    s = "hello​world"  # contains a Zero Width Space
    # ZWSP is NOT in the strip set — it remains.
    assert "​" in ax_assert._strip_bidi(s)


# ---------------------------------------------------------------------------
# assert_focused_chat_matches
# ---------------------------------------------------------------------------


def test_pyobjc_unavailable_raises_accessibility_api_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_PYOBJC_AVAILABLE = False`` (D-06 fallback) → AccessibilityAPIUnavailable."""
    monkeypatch.setattr(ax_assert, "_PYOBJC_AVAILABLE", False)
    with pytest.raises(AccessibilityAPIUnavailable, match="pyobjc"):
        ax_assert.assert_focused_chat_matches("Alice")


def test_whatsapp_not_running_raises_chat_header_mismatch(
    mock_pyobjc: object,
) -> None:
    """``runningApplications`` returns no WhatsApp → ChatHeaderMismatch."""
    # mock_pyobjc returns the _AXFake; access via cast.
    from tests.unit.conftest import _AXFake

    fake: _AXFake = mock_pyobjc  # type: ignore[assignment]
    fake.whatsapp_running = False

    with pytest.raises(ChatHeaderMismatch, match="not running"):
        ax_assert.assert_focused_chat_matches("Alice")


def test_focused_window_lookup_fail_raises_chat_header_mismatch(
    mock_pyobjc: object,
) -> None:
    """``AXFocusedWindow`` returns non-zero err → ChatHeaderMismatch."""
    from tests.unit.conftest import _AXFake

    fake: _AXFake = mock_pyobjc  # type: ignore[assignment]
    # Simulate the AX error -25212 (kAXErrorCannotComplete) on focused-window lookup.
    fake.focused_window_err = -25212

    with pytest.raises(ChatHeaderMismatch, match="AXFocusedWindow"):
        ax_assert.assert_focused_chat_matches("Alice")


def test_matching_heading_returns_cleanly(mock_pyobjc: object) -> None:
    """A heading whose stripped/casefolded form contains the expected name → returns None."""
    from tests.unit.conftest import _AXFake

    fake: _AXFake = mock_pyobjc  # type: ignore[assignment]
    fake.walk_returns = ["‎⁨Olivier Giffard⁩"]

    # Should NOT raise — the stripped heading "Olivier Giffard" contains
    # the expected name as a substring.
    ax_assert.assert_focused_chat_matches("Olivier Giffard")


def test_non_matching_heading_raises_chat_header_mismatch(
    mock_pyobjc: object,
) -> None:
    """A heading that doesn't contain the expected name → ChatHeaderMismatch."""
    from tests.unit.conftest import _AXFake

    fake: _AXFake = mock_pyobjc  # type: ignore[assignment]
    fake.walk_returns = ["Mom"]

    with pytest.raises(ChatHeaderMismatch, match="does not match"):
        ax_assert.assert_focused_chat_matches("Momentum project")


def test_strip_bidi_casefold_substring_match(mock_pyobjc: object) -> None:
    """Lowercase expected name matches mixed-case observed heading via casefold."""
    from tests.unit.conftest import _AXFake

    fake: _AXFake = mock_pyobjc  # type: ignore[assignment]
    fake.walk_returns = ["Alice In Wonderland"]

    # Expected lowercase; observed mixed-case. Should match.
    ax_assert.assert_focused_chat_matches("alice in wonderland")


def test_walk_caps_at_200_nodes(mock_pyobjc: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """DoS guard: the DFS visits at most ``_MAX_WALK_NODES`` (200) nodes.

    Build a pathological focused-window AX tree where the root has 500
    direct children, all of which are AXHeading roles but with
    non-matching descriptions. The walk MUST terminate at ≤200 nodes;
    expected behavior is ``ChatHeaderMismatch`` (no match found within
    the visited budget) rather than infinite loop.
    """
    from tests.unit.conftest import _AXFake

    fake: _AXFake = mock_pyobjc  # type: ignore[assignment]
    # 500 heading nodes — exceeds the 200-node DFS budget.
    fake.walk_returns = [f"Heading-{i}" for i in range(500)]

    with pytest.raises(ChatHeaderMismatch):
        ax_assert.assert_focused_chat_matches("Nonexistent Chat Name")

    # role_calls should have plateaued at the DFS cap (200 nodes).
    # The walk explicitly bounds at _MAX_WALK_NODES per the source.
    assert fake.role_calls <= ax_assert._MAX_WALK_NODES + 1


# ---------------------------------------------------------------------------
# WhatsApp 26.31.23 chat-header shape (verified live, pid 948)
# ---------------------------------------------------------------------------


def _wa_26_31_23_window(
    focused: str,
    *,
    call_target: str | None = None,
    backlog: int = 3,
) -> list[dict[str, object]]:
    """The 26.31.23 focused-window AX shape, verified live (pid 948, 81 nodes).

    Sidebar branch: the window's ONLY ``AXHeading`` is the sidebar title
    "Chats" — NOT the focused chat — and the chat rows are ``AXButton``
    nodes carrying other chats' names with no ``AXIdentifier`` at all.

    Conversation branch: the header is an ``AXButton`` carrying
    ``AXIdentifier == "NavigationBar_HeaderViewButton"``; the call buttons
    beside it carry no identifier; the message bubbles are ``AXStaticText``
    under ``AXIdentifier == "WAMessageBubbleTableViewCell"``. The header
    sits behind the message list in depth-first order, which is what made
    the 200-node budget bury it.
    """
    call_target = focused if call_target is None else call_target
    return [
        {
            "role": "AXGroup",
            "children": [
                {
                    "role": "AXGroup",
                    "identifier": "Toolbar",
                    "children": [
                        {"role": "AXHeading", "label": "‎Chats"},
                        {
                            "role": "AXButton",
                            "identifier": "NavigationBar_NewChatButton",
                            "label": "‎New Chat",
                        },
                    ],
                },
                {
                    "role": "AXGroup",
                    "identifier": "ChatListView_TableView",
                    "children": [
                        {"role": "AXButton", "label": "CRED"},
                        {"role": "AXButton", "label": "Nitin Stable Money"},
                        {"role": "AXButton", "label": "Engineering - StableMoney"},
                    ],
                },
            ],
        },
        {
            "role": "AXGroup",
            "children": [
                {
                    "role": "AXGroup",
                    "identifier": "Toolbar",
                    "children": [
                        {
                            "role": "AXButton",
                            "identifier": "NavigationBar_HeaderViewButton",
                            "label": focused,
                        },
                        {
                            "role": "AXButton",
                            "label": f"‎Start video call with {call_target}",
                        },
                    ],
                },
                {
                    "role": "AXGroup",
                    "identifier": "ChatMessagesTableView",
                    "children": [
                        {
                            "role": "AXStaticText",
                            "identifier": "WAMessageBubbleTableViewCell",
                            "label": f"‎Your message, m{i}, ‎Sent to {focused}",
                        }
                        for i in range(backlog)
                    ],
                },
            ],
        },
    ]


def test_chat_header_axbutton_identifier_matches_on_26_31_23(
    mock_pyobjc: object,
) -> None:
    """VERIFIED-LIVE regression (26.31.23): the header is an AXButton, not an AXHeading.

    Fails before the fix: the AXHeading-only walk collects the sidebar
    title "Chats" and nothing else, so the preflight can never match the
    focused chat on this build and every send aborts.
    """
    from tests.unit.conftest import _AXFake

    fake: _AXFake = mock_pyobjc  # type: ignore[assignment]
    fake.walk_tree = _wa_26_31_23_window("Nitin Stable Money")

    # Should NOT raise — the NavigationBar_HeaderViewButton node carries
    # the focused chat's display name.
    ax_assert.assert_focused_chat_matches("Nitin Stable Money")


def test_chat_header_identifier_label_lrm_is_normalised(mock_pyobjc: object) -> None:
    """U+200E on the identifier-selected header label is stripped before comparing."""
    from tests.unit.conftest import _AXFake

    fake: _AXFake = mock_pyobjc  # type: ignore[assignment]
    # Leading LRM on the header label — the form most 26.31.23 AX labels carry.
    fake.walk_tree = _wa_26_31_23_window("‎⁨Nitin Stable Money⁩")

    ax_assert.assert_focused_chat_matches("Nitin Stable Money")


def test_wrong_chat_aborts_despite_sidebar_row_and_call_button(
    mock_pyobjc: object,
) -> None:
    """D-03: chat B focused, send aimed at chat A → abort, even though A is on screen.

    "Nitin Stable Money" is visible as a sidebar row AND named by the
    conversation's call button, but neither is the focused chat. Only the
    NavigationBar_HeaderViewButton node may satisfy the assertion.
    """
    from tests.unit.conftest import _AXFake

    fake: _AXFake = mock_pyobjc  # type: ignore[assignment]
    fake.walk_tree = _wa_26_31_23_window("Mom", call_target="Nitin Stable Money")

    with pytest.raises(ChatHeaderMismatch) as exc_info:
        ax_assert.assert_focused_chat_matches("Nitin Stable Money")

    # The diagnostic must show the header only — not the sidebar rows, not
    # the sidebar title, not the call button.
    assert "['Mom']" in str(exc_info.value)


def test_long_message_backlog_does_not_bury_the_chat_header(
    mock_pyobjc: object,
) -> None:
    """The 200-node budget must not be spent on the message list before the header.

    Fails with the old LIFO walk even once the header node is selectable:
    depth-first descends into the 250-bubble message subtree first and
    exhausts ``_MAX_WALK_NODES`` before reaching the shallow toolbar, which
    is what produced the empty ``observed ... = []`` diagnostic in the
    field report.
    """
    from tests.unit.conftest import _AXFake

    fake: _AXFake = mock_pyobjc  # type: ignore[assignment]
    fake.walk_tree = _wa_26_31_23_window("Nitin Stable Money", backlog=250)

    ax_assert.assert_focused_chat_matches("Nitin Stable Money")


def _sidebar_tree(monkeypatch: pytest.MonkeyPatch, rows: list[tuple[str, str]]) -> list[object]:
    """Build a fake sidebar of ``(role, label)`` rows; return the press log.

    The press log records the node that ``AXPress`` was fired on, so a test
    can assert WHICH row was opened rather than merely that something was.
    """

    class Node:
        def __init__(self, role: str, label: str) -> None:
            self.role = role
            self.label = label

    nodes = [Node(role, label) for role, label in rows]
    root = Node("AXWindow", "")
    pressed: list[object] = []

    def fake_copy(elem: object, attr: object, _none: object) -> tuple[int, object]:
        if attr == kAXFocusedWindowAttribute:
            return (0, root)
        if attr == kAXChildrenAttribute:
            return (0, nodes if elem is root else [])
        if attr == kAXRoleAttribute:
            return (0, getattr(elem, "role", "AXWindow"))
        if attr == kAXDescriptionAttribute:
            return (0, getattr(elem, "label", ""))
        return (-1, None)

    monkeypatch.setattr(ax_assert, "_PYOBJC_AVAILABLE", True)
    monkeypatch.setattr(ax_assert, "_resolve_whatsapp_pid", lambda: 948)
    monkeypatch.setattr(ax_assert, "AXUIElementCreateApplication", lambda _pid: object())
    monkeypatch.setattr(ax_assert, "AXUIElementCopyAttributeValue", fake_copy)

    def fake_press(elem: object, _action: str) -> int:
        pressed.append(elem)
        return 0

    monkeypatch.setattr(ax_assert, "AXUIElementPerformAction", fake_press)
    ax_assert._sidebar_nodes_for_test = nodes  # type: ignore[attr-defined]
    return pressed


def test_open_first_search_result_presses_the_matched_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row that was verified is the row that gets opened.

    Verified-live regression: with the search field focused and the
    expected chat the topmost result, Return did NOT select it — the
    previously-open chat stayed and the send aborted on the header assert.
    """
    pressed = _sidebar_tree(
        monkeypatch,
        [("AXHeading", "\u200eChats"), ("AXButton", "reminder"), ("AXButton", "reminders club")],
    )

    ax_assert.open_first_search_result("reminder")

    nodes = ax_assert._sidebar_nodes_for_test  # type: ignore[attr-defined]
    assert pressed == [nodes[1]], "must press the first matching row, not a later one"


def test_open_first_search_result_never_presses_a_heading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headings satisfy the diagnostic label set but are not openable rows."""
    pressed = _sidebar_tree(monkeypatch, [("AXHeading", "reminder")])

    with pytest.raises(ChatHeaderMismatch, match="topmost result does not match"):
        ax_assert.open_first_search_result("reminder")

    assert pressed == []


def test_open_first_search_result_raises_without_pressing_on_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong-chat sidebar must abort with the observed labels, pressing nothing."""
    pressed = _sidebar_tree(monkeypatch, [("AXButton", "Papa"), ("AXButton", "CRED")])

    with pytest.raises(ChatHeaderMismatch, match=r"\['Papa', 'CRED'\]"):
        ax_assert.open_first_search_result("reminder")

    assert pressed == []


def test_assert_focused_chat_matches_error_message_includes_observed_headings(
    mock_pyobjc: object,
) -> None:
    """On mismatch, the exception message names the stripped observed headings."""
    from tests.unit.conftest import _AXFake

    fake: _AXFake = mock_pyobjc  # type: ignore[assignment]
    fake.walk_returns = ["‎Alice Smith", "‎Bob Jones"]

    with pytest.raises(ChatHeaderMismatch) as exc_info:
        ax_assert.assert_focused_chat_matches("Carol Williams")

    msg = str(exc_info.value)
    assert "Carol Williams" in msg
    # The stripped names should appear in the diagnostic.
    assert "Alice Smith" in msg
    assert "Bob Jones" in msg


# ---------------------------------------------------------------------------
# Transient-sheet retry (verified live on WhatsApp 26.31.23)
# ---------------------------------------------------------------------------
#
# While a deep-link opens a chat, WhatsApp raises a 4-node AXSheet over the
# chat window. AXFocusedWindow resolves to that sheet, whose subtree has no
# chat header, so the preflight observed an EMPTY candidate list and aborted
# a send whose compose box was already prefilled. Retrying on empty fixes
# that WITHOUT weakening the wrong-chat guard: a non-empty mismatch still
# aborts on the first look.


def _stub_ax(monkeypatch: pytest.MonkeyPatch, walk_results: list[list[str]]) -> dict[str, int]:
    """Drive assert_focused_chat_matches with a scripted sequence of walks."""
    calls = {"walk": 0, "sleep": 0}

    monkeypatch.setattr(ax_assert, "_PYOBJC_AVAILABLE", True)
    monkeypatch.setattr(ax_assert, "_resolve_whatsapp_pid", lambda: 948)
    monkeypatch.setattr(ax_assert, "AXUIElementCreateApplication", lambda _pid: object())
    monkeypatch.setattr(ax_assert, "AXUIElementCopyAttributeValue", lambda *_a, **_k: (0, object()))

    def fake_sleep(_seconds: float) -> None:
        calls["sleep"] += 1

    monkeypatch.setattr("whatsapp_desktop_mcp.sender.ax_assert.time.sleep", fake_sleep)

    def fake_walk(*_a: object, **_k: object) -> list[str]:
        idx = min(calls["walk"], len(walk_results) - 1)
        calls["walk"] += 1
        return walk_results[idx]

    monkeypatch.setattr(ax_assert, "_walk_for_heading", fake_walk)
    return calls


def test_transient_sheet_empty_walk_is_retried_until_header_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty candidate list is the sheet case — retry, then match."""
    calls = _stub_ax(monkeypatch, [[], [], ["Nitin Stable Money"]])

    ax_assert.assert_focused_chat_matches("Nitin Stable Money")

    assert calls["walk"] == 3, "should have retried past the two empty walks"
    assert calls["sleep"] == 2, "should back off between retries"


def test_wrong_chat_still_aborts_on_the_first_look(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-03 guard unchanged: a NON-empty mismatch must not be retried."""
    calls = _stub_ax(monkeypatch, [["Mom"], ["Nitin Stable Money"]])

    with pytest.raises(ChatHeaderMismatch) as excinfo:
        ax_assert.assert_focused_chat_matches("Nitin Stable Money")

    assert calls["walk"] == 1, "a readable wrong chat must fail fast, not retry"
    assert calls["sleep"] == 0
    assert "'Mom'" in str(excinfo.value)


def test_persistently_unreadable_header_exhausts_retries_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the header never becomes readable, the send still aborts."""
    calls = _stub_ax(monkeypatch, [[]])

    with pytest.raises(ChatHeaderMismatch):
        ax_assert.assert_focused_chat_matches("Nitin Stable Money")

    assert calls["walk"] == ax_assert._HEADER_SETTLE_ATTEMPTS


# ---------------------------------------------------------------------------
# focus_composer — verified live on WhatsApp 26.31.23
# ---------------------------------------------------------------------------
#
# Return-selecting a sidebar search result opens the chat but leaves
# AXFocusedUIElement on TokenizedSearchBar_TextView. The group image send
# fired Cmd-V into the search field: the chat was open and the clipboard held
# the image, but nothing was sent and no ZWAMESSAGE row appeared.


def _stub_focus(
    monkeypatch: pytest.MonkeyPatch,
    *,
    composer_found: bool,
    focused_ids: list[str | None],
) -> dict[str, int]:
    """Drive focus_composer with a scripted AXFocusedUIElement identifier sequence.

    ``focused_ids`` is read one entry per settle attempt; the last entry
    repeats once exhausted.
    """
    calls = {"press": 0, "sleep": 0, "focus_read": 0}
    composer = object()

    monkeypatch.setattr(ax_assert, "_PYOBJC_AVAILABLE", True)
    monkeypatch.setattr(ax_assert, "_resolve_whatsapp_pid", lambda: 948)
    monkeypatch.setattr(ax_assert, "AXUIElementCreateApplication", lambda _pid: object())
    monkeypatch.setattr(
        ax_assert,
        "_find_by_identifier",
        lambda _elem, _ident: composer if composer_found else None,
    )

    def fake_sleep(_seconds: float) -> None:
        calls["sleep"] += 1

    monkeypatch.setattr("whatsapp_desktop_mcp.sender.ax_assert.time.sleep", fake_sleep)

    def fake_press(_elem: object, _action: str) -> int:
        calls["press"] += 1
        return 0

    monkeypatch.setattr(ax_assert, "AXUIElementPerformAction", fake_press)

    focused_marker = object()

    def fake_copy(_elem: object, attr: object, _none: object) -> tuple[int, object]:
        if attr == kAXFocusedWindowAttribute:
            return (0, object())
        if attr == kAXFocusedUIElementAttribute:
            calls["focus_read"] += 1
            return (0, focused_marker)
        if attr == kAXIdentifierAttribute:
            idx = min(calls["focus_read"] - 1, len(focused_ids) - 1)
            value = focused_ids[idx]
            return (0, value) if value is not None else (-1, None)
        return (0, object())

    monkeypatch.setattr(ax_assert, "AXUIElementCopyAttributeValue", fake_copy)
    return calls


def test_focus_composer_returns_once_focus_lands_on_the_composer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Focus is applied asynchronously — the search field is read first, then the composer."""
    calls = _stub_focus(
        monkeypatch,
        composer_found=True,
        focused_ids=["TokenizedSearchBar_TextView", ax_assert._COMPOSER_IDENTIFIER],
    )

    ax_assert.focus_composer()

    assert calls["press"] == 1
    assert calls["focus_read"] == 2  # retried past the stale search-field read


def test_focus_composer_raises_when_focus_never_leaves_the_search_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live failure: focus stuck on the search bar must ABORT, never type."""
    calls = _stub_focus(
        monkeypatch,
        composer_found=True,
        focused_ids=["TokenizedSearchBar_TextView"],
    )

    with pytest.raises(ComposerNotFocused, match="AXFocusedUIElement never"):
        ax_assert.focus_composer()

    assert calls["focus_read"] == ax_assert._FOCUS_SETTLE_ATTEMPTS


def test_focus_composer_raises_when_the_composer_node_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ChatBar_ComposerTextView under the focused window → abort, do not guess."""
    _stub_focus(monkeypatch, composer_found=False, focused_ids=[])

    with pytest.raises(ComposerNotFocused, match="ChatBar_ComposerTextView"):
        ax_assert.focus_composer()


def test_focus_composer_raises_when_pyobjc_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-06 fallback: a broken pyobjc install surfaces as AccessibilityAPIUnavailable."""
    monkeypatch.setattr(ax_assert, "_PYOBJC_AVAILABLE", False)

    with pytest.raises(AccessibilityAPIUnavailable):
        ax_assert.focus_composer()


def test_find_by_identifier_is_breadth_first_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deep decoy subtree must not bury the shallow composer, and the walk is capped."""

    class Node:
        def __init__(self, ident: str | None, kids: list[Node] | None = None) -> None:
            self.ident = ident
            self.kids = kids or []

    # A long chain (the message list) enqueued BEFORE the shallow composer:
    # DFS would exhaust the budget inside it; BFS reaches the composer at depth 1.
    chain: Node = Node("deep-leaf")
    for _ in range(ax_assert._MAX_WALK_NODES * 2):
        chain = Node("deep", [chain])
    composer = Node(ax_assert._COMPOSER_IDENTIFIER)
    root = Node("root", [chain, composer])

    def fake_copy(elem: Node, attr: object, _none: object) -> tuple[int, object]:
        if attr == kAXIdentifierAttribute:
            return (0, elem.ident)
        if attr == kAXChildrenAttribute:
            return (0, elem.kids)
        return (-1, None)

    monkeypatch.setattr(ax_assert, "AXUIElementCopyAttributeValue", fake_copy)

    assert ax_assert._find_by_identifier(root, ax_assert._COMPOSER_IDENTIFIER) is composer
    assert ax_assert._find_by_identifier(root, "not-present") is None


# ---------------------------------------------------------------------------
# _resolve_whatsapp_pid — must query live, never a cached list
# ---------------------------------------------------------------------------


def test_resolve_whatsapp_pid_queries_live_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WhatsApp started AFTER the server must still be found.

    Verified-live regression: ``NSWorkspace.runningApplications()`` is
    maintained by run-loop notifications this server never pumps, so it
    froze at first access. The server was started while WhatsApp was closed
    and then reported "WhatsApp.app is not running" for the rest of its life
    while WhatsApp sat in the Dock.
    """
    monkeypatch.setattr(ax_assert, "_PYOBJC_AVAILABLE", True)

    class _App:
        def processIdentifier(self) -> int:  # noqa: N802 — match pyobjc name
            return 38497

    launched: list[bool] = [False]
    queries: list[str] = []

    class _Shim:
        @staticmethod
        def runningApplicationsWithBundleIdentifier_(bundle_id: str) -> list[_App]:  # noqa: N802
            queries.append(bundle_id)
            return [_App()] if launched[0] else []

    monkeypatch.setattr(ax_assert, "NSRunningApplication", _Shim)

    assert ax_assert._resolve_whatsapp_pid() is None  # not running yet
    launched[0] = True
    assert ax_assert._resolve_whatsapp_pid() == 38497  # started since — found

    assert queries == [ax_assert._WHATSAPP_BUNDLE_ID] * 2, "must re-query, not cache"


def test_ax_assert_does_not_use_nsworkspace() -> None:
    """Pin the API choice: NSWorkspace's list is stale in a run-loop-less process."""
    assert not hasattr(ax_assert, "NSWorkspace")
