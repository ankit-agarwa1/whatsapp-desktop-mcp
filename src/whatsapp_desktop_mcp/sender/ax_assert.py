"""macOS Accessibility-API state assertion for the WhatsApp send path.

This module is the **load-bearing P5 mitigation** against the wrong-chat
fuzzy-send class of bugs (CONTEXT.md D-03 / SEND-04). Before any keystroke
fires from the sender, this module reads the currently-focused WhatsApp
window's chat header via the macOS Accessibility API (pyobjc binding for
``ApplicationServices``) and compares it against the chat name the upstream
tool layer resolved from ``chat_id``. If the focused chat does NOT match —
because the user manually switched chats between the resolve step and the
send step, OR because the deep-link opened a different chat than the URL
encoded, OR because WhatsApp itself navigated due to an incoming
notification — the send is aborted with :class:`ChatHeaderMismatch` and the
keystroke never runs.

**Why pyobjc and not an ``osascript`` AX walk:** verified live the
``osascript`` AX walk works but takes ~150–300 ms per probe (vs ~5–15 ms
for pyobjc). Pre-send latency dominates the send experience; the AX walk
runs on the hot path and we want it cheap.

**Why D-06 try/except ImportError:** if the user's pyobjc install is
broken (wrong arch, .dylib resolution failure, etc.), we MUST NOT crash
the entire MCP server at import time — the read tools must keep working
even if pyobjc is unhappy. The top-of-module pyobjc imports are wrapped
in ``try/except ImportError``; on failure ``_PYOBJC_AVAILABLE`` is set to
``False`` and both public functions raise the structured
:class:`AccessibilityAPIUnavailable` exception (NOT a Python traceback) so
the upstream tool surface can map it to a clean MCP error.

**Three bidi invisibles to strip** (verified live on WhatsApp 26.16.74,
2026-05-13 — see ``02-RESEARCH.md §"Pattern 2"`` and SP-3 spike):

* U+200E LRM — Left-to-Right Mark (the most common, prefixed on every
  user-visible label)
* U+2068 FSI — First Strong Isolate
* U+2069 PDI — Pop Directional Isolate

All three are declared via ``\\uNNNN`` escape literal form (NOT raw
characters) so the source file stays grep-stable: the raw characters
would render as zero-width invisibles in source viewers and would pollute
downstream literal-token greps with ghost matches.

**Bounded breadth-first walk** (vs. hardcoded attribute path): the chat
header sits at variable depth in the AXGroup tree — observed live on
26.31.23 at ``AXWindow → AXGroup → AXGroup → AXGroup(Toolbar) →
AXButton``, with depth depending on whether the sidebar is collapsed.
The walk caps at ``_MAX_WALK_NODES = 200`` visited nodes (DoS guard
T-02-01-04); a pathological window with millions of nodes would be
aborted with a "no match" result (raising :class:`ChatHeaderMismatch`)
rather than spinning forever and freeing the OOM/CPU bomb scenario.

The traversal is breadth-first (FIFO) on purpose. Depth-first (LIFO)
descends into the last-pushed subtree first, which is the message list —
one AX node per rendered bubble — so on a chat with a long rendered
backlog the 200-node budget was exhausted *before* the walk ever reached
the shallow chrome holding the header, and the preflight reported an
empty observed list. Breadth-first reaches the header within the first
~15 pops regardless of backlog size, which makes the cap a pure cost
bound instead of a correctness hazard.

**Casefold + substring** (vs. equality): the focused chat header in
WhatsApp Catalyst may carry a locale-dependent suffix
("Olivier Giffard • online" / "Last seen today" / "typing…"). After
stripping the three bidi invisibles and casefolding both sides, a
substring match accommodates locale variation while still failing on
the wrong-chat scenario (a completely different chat name will not
substring-match).

**SP-3 locked role filter:** only ``AXHeading`` is collected by role in
the default walk. Widening to ``AXStaticText`` would catastrophically
false-positive on message body content (any message bubble containing
the expected chat name would falsely "match" the chat header).

**Identifier-selected header (WhatsApp ≥ 26.31.23):** SP-3 is honoured —
the role set is NOT widened. On 26.31.23 the focused-chat header is no
longer an ``AXHeading`` at all: it is an ``AXButton`` carrying
``AXIdentifier == "NavigationBar_HeaderViewButton"``, while the window's
only ``AXHeading`` is the sidebar title "Chats". The default walk
therefore also selects nodes by that exact AXIdentifier (see
``_CHAT_HEADER_IDENTIFIER``), and prefers identifier-selected labels over
role-selected ones when both are present. Selecting on an exact
identifier is strictly *narrower* than the role filter, not wider: it
cannot admit message bubbles (``WAMessageBubbleTableViewCell``), sidebar
chat rows (no identifier at all), the call buttons beside the header (no
identifier), or "New Chat" (``NavigationBar_NewChatButton``) — all
verified live on 26.31.23, pid 948.

**SP-5 locked role widening for the group-fallback first-result
preflight:** ``_assert_first_search_result_matches`` calls the same
DFS with the widened set ``{"AXHeading", "AXButton"}`` because the
first clickable sidebar result is an ``AXButton`` whose
``AXDescription`` carries the chat display name (with leading U+200E).

**SP-4 locked return tuple:** every
``AXUIElementCopyAttributeValue(elem, attr, None)`` call returns the
2-tuple ``(err: int, value)`` under pyobjc 12.1.

**REL-05 D-24 isolation:** this module imports nothing from the project's
read-side data tier (no DB connection helpers, no message accessors, no
schema probes). The only intra-project import is
:mod:`whatsapp_desktop_mcp.exceptions` for the structured error classes. The
sender → connection-helper edge that ships in Plan 02-03's verify module
is intentionally NOT a dependency of the AX preflight.

**Sync (not async):** the pyobjc AX-API calls are CPU-bound C extensions;
wrapping them in ``asyncio.to_thread`` adds latency without benefit. The
Plan 02-03 orchestrator calls these functions from inside its own
``asyncio.to_thread`` if it wants to keep the event loop responsive; for
v0.1 the AX preflight latency (~5–15 ms) is small enough to run inline
without yielding.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from whatsapp_desktop_mcp.exceptions import (
    AccessibilityAPIUnavailable,
    ChatHeaderMismatch,
    ComposerNotFocused,
)

logger = logging.getLogger(__name__)

# D-06 — pyobjc imports wrapped in try/except so a broken install on the
# user's machine does NOT crash the entire MCP server at import time. The
# read tools keep working; only the AX preflight surfaces as an
# AccessibilityAPIUnavailable error.
try:
    from ApplicationServices import (  # type: ignore[import-untyped]
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXUIElementPerformAction,
        kAXChildrenAttribute,
        kAXDescriptionAttribute,
        kAXFocusedUIElementAttribute,
        kAXFocusedWindowAttribute,
        kAXIdentifierAttribute,
        kAXRoleAttribute,
        kAXTitleAttribute,
    )
    from Cocoa import NSRunningApplication  # type: ignore[import-untyped]

    _PYOBJC_AVAILABLE = True
except ImportError:
    _PYOBJC_AVAILABLE = False


# The three bidi invisibles WhatsApp Catalyst injects into AX labels.
# Declared via Python \uNNNN escape literal form (NOT raw characters) so
# this source file stays grep-stable: raw zero-width characters would render
# as zero-width invisibles in source viewers and pollute downstream literal-
# token grep gates with ghost matches. The escape literals \u200E / \u2068
# / \u2069 are interpreted by the Python parser at module-load time into
# the same three codepoints WhatsApp emits in AX labels, so set membership
# tests against AX-extracted strings work as expected.
#
#   \u200E LRM — Left-to-Right Mark (most common; prefixes every label)
#   \u2068 FSI — First Strong Isolate
#   \u2069 PDI — Pop Directional Isolate
_INVISIBLE_BIDI: frozenset[str] = frozenset({"\u200e", "\u2068", "\u2069"})


# WhatsApp Desktop's bundle identifier (verified live on WhatsApp 26.16.74).
_WHATSAPP_BUNDLE_ID = "net.whatsapp.WhatsApp"


# Bounded depth-first walk cap (T-02-01-04 DoS guard). The chat header sits
# at variable depth — observed live ~50–80 nodes total under the focused
# window in sidebar-only mode, more when a chat is open. 200 is a generous
# margin; if a pathological window had more nodes, exhausting this budget
# falls through to ChatHeaderMismatch (safer than OOM).
_MAX_WALK_NODES = 200


# Default narrow role filter for the focused-chat preflight. SP-3 locked
# this set: AXHeading only — widening to AXStaticText would
# catastrophically false-positive on message body text.
_DEFAULT_HEADING_ROLES: frozenset[str] = frozenset({"AXHeading"})


# Exact AXIdentifier of the focused-chat header node (verified live on
# WhatsApp 26.31.23, pid 948: the single node in the focused window carrying
# it, an AXButton whose AXDescription is the chat display name). This is a
# selector, NOT an SP-3 role widening — no extra role is admitted, so the
# message-body false-positive class SP-3 guards against stays impossible.
# On 26.16.74 no node carries it and the walk falls back to AXHeading.
_CHAT_HEADER_IDENTIFIER = "NavigationBar_HeaderViewButton"

# The chat's message-composer text view. Exactly one node in the focused
# window carries this AXIdentifier (verified live on WhatsApp 26.31.23).
_COMPOSER_IDENTIFIER = "ChatBar_ComposerTextView"

# Focus is set through the AX API, which WhatsApp applies asynchronously;
# re-read AXFocusedUIElement until it lands. Same shape as the header
# settle loop above.
_FOCUS_SETTLE_ATTEMPTS = 10
_FOCUS_SETTLE_INTERVAL_S = 0.05


# Widened role filter for the sidebar-search first-result preflight. SP-5
# locked this set: the topmost clickable search result is an AXButton
# whose AXDescription carries the chat display name (verified live).
_SIDEBAR_RESULT_ROLES: frozenset[str] = frozenset({"AXHeading", "AXButton"})


# Retry budget for an UNREADABLE focused-chat header. WhatsApp raises a
# transient AXSheet over the chat window while a deep-link opens (verified
# live on 26.31.23: a 4-node sheet, where the settled window walks ~84
# nodes). AXFocusedWindow resolves to that sheet, whose subtree holds no
# chat header at all, so a single-shot preflight aborted sends that were
# about to be correct — the deep-link had already prefilled the compose
# box and only the Return keystroke was missing.
#
# 10 x 0.15 s = 1.35 s worst case, comfortably inside the send tool's
# budget and only paid when the header is unreadable.
_HEADER_SETTLE_ATTEMPTS = 10
_HEADER_SETTLE_INTERVAL_S = 0.15


def _strip_bidi(s: str) -> str:
    """Strip the three known bidi invisibles WhatsApp Catalyst inserts.

    Casefolding is deliberately NOT applied here — callers apply it at
    the comparison site so display/audit paths can keep original case.
    """
    return "".join(c for c in s if c not in _INVISIBLE_BIDI).strip()


def _resolve_whatsapp_pid() -> int | None:
    """Resolve WhatsApp Desktop's PID, queried live on every call.

    Uses ``NSRunningApplication.runningApplicationsWithBundleIdentifier_``,
    NOT ``NSWorkspace.sharedWorkspace().runningApplications()``. The latter
    is maintained by workspace notifications delivered on the main run loop;
    this server never spins one, so the list is a snapshot frozen at first
    access. A server that starts while WhatsApp is closed then stays blind to
    it forever, and every send fails with "WhatsApp.app is not running" while
    WhatsApp sits in the Dock. Verified live — after launching a fresh app:

        NSWorkspace.runningApplications()          -> [9213]   (stale)
        runningApplicationsWithBundleIdentifier_   -> [38839]
        pgrep                                      -> 38839

    Returns ``None`` when:
      * pyobjc is not available (``_PYOBJC_AVAILABLE == False``), OR
      * WhatsApp.app is not currently running.

    The caller distinguishes these two cases at its own discretion — the
    public functions raise :class:`AccessibilityAPIUnavailable` for the
    first and :class:`ChatHeaderMismatch` for the second.
    """
    if not _PYOBJC_AVAILABLE:
        return None
    running = NSRunningApplication.runningApplicationsWithBundleIdentifier_(_WHATSAPP_BUNDLE_ID)
    for app in running or []:
        return int(app.processIdentifier())
    return None


def _walk_for_heading(
    elem: Any,
    *,
    roles: frozenset[str] = _DEFAULT_HEADING_ROLES,
    identifier: str | None = None,
) -> list[str]:
    """Bounded breadth-first walk; collect AX-label strings under ``elem``.

    For every node whose ``AXRole`` is in ``roles`` — or, when
    ``identifier`` is given, whose ``AXIdentifier`` equals it exactly —
    both ``AXDescription`` and ``AXTitle`` are read and any non-empty
    string value is collected. Children are enqueued via ``AXChildren``.

    When ``identifier`` matched at least one node, ONLY the
    identifier-selected labels are returned; the role-selected ones are
    dropped. On 26.31.23 the role-selected set is the sidebar title
    ("Chats"), which is not the focused chat and must not be offered to
    the caller's match loop as if it were.

    SP-3-locked default roles: ``{"AXHeading"}``. SP-5-widened roles for
    the sidebar-result preflight: ``{"AXHeading", "AXButton"}``.

    Breadth-first (``pop(0)``), not depth-first: LIFO descended into the
    message-list subtree first and burned the node budget before reaching
    the shallow header — see the module docstring.

    The walk is bounded at ``_MAX_WALK_NODES`` visited nodes; exhaustion
    returns whatever was collected so far (the caller's
    :class:`ChatHeaderMismatch` raise will surface as "no match found").
    """
    headings: list[str] = []
    by_identifier: list[str] = []
    queue: list[Any] = [elem]
    visited = 0
    while queue and visited < _MAX_WALK_NODES:
        node = queue.pop(0)
        visited += 1

        # SP-4 locked return shape: AXUIElementCopyAttributeValue returns
        # tuple[err: int, value]. err == 0 means success.
        matched_identifier = False
        if identifier is not None:
            id_err, node_id = AXUIElementCopyAttributeValue(node, kAXIdentifierAttribute, None)
            matched_identifier = id_err == 0 and node_id == identifier

        role_err, role = AXUIElementCopyAttributeValue(node, kAXRoleAttribute, None)
        if matched_identifier or (role_err == 0 and role in roles):
            sink = by_identifier if matched_identifier else headings
            desc_err, desc = AXUIElementCopyAttributeValue(node, kAXDescriptionAttribute, None)
            if desc_err == 0 and isinstance(desc, str) and desc:
                sink.append(desc)
            title_err, title = AXUIElementCopyAttributeValue(node, kAXTitleAttribute, None)
            if title_err == 0 and isinstance(title, str) and title:
                sink.append(title)

        kids_err, kids = AXUIElementCopyAttributeValue(node, kAXChildrenAttribute, None)
        if kids_err == 0 and kids:
            # __NSArrayM iterates as a Python list of AXUIElementRef.
            queue.extend(kids)

    return by_identifier or headings


def assert_focused_chat_matches(expected_chat_name: str) -> None:
    """Verify WhatsApp's currently-focused chat header matches the expected name.

    Algorithm (D-03 / SEND-04):

    1. If pyobjc is unavailable (D-06 fallback), raise
       :class:`AccessibilityAPIUnavailable`.
    2. Resolve WhatsApp.app's PID (queried live). If WhatsApp is not
       running, raise :class:`ChatHeaderMismatch`.
    3. Create an AX element for the application, read its
       ``AXFocusedWindow`` attribute. On failure, raise
       :class:`ChatHeaderMismatch`.
    4. Walk the focused window's AX tree (bounded BFS at 200 nodes) and
       collect the chat-header description/title: the node whose
       ``AXIdentifier`` is ``NavigationBar_HeaderViewButton`` (WhatsApp
       ≥ 26.31.23) if present, else every ``AXHeading`` (≤ 26.16.74).
    5. Strip bidi invisibles + casefold both sides. If the expected name
       (after strip + casefold) appears as a substring of ANY observed
       heading (after strip + casefold), the send is safe — return.
    6. Otherwise raise :class:`ChatHeaderMismatch` with the expected name
       and the stripped observed headings in the message.

    Substring (not equality) accommodates WhatsApp's locale-dependent
    header suffixes ("• online", "Last seen today", "typing…"). The
    expected name must appear in full; partial matches that span the
    suffix do not occur in practice.
    """
    if not _PYOBJC_AVAILABLE:
        raise AccessibilityAPIUnavailable(
            "pyobjc not available; cannot perform AX preflight — reinstall "
            "pyobjc-core, pyobjc-framework-Cocoa, and "
            "pyobjc-framework-ApplicationServices to enable wrong-chat protection"
        )

    pid = _resolve_whatsapp_pid()
    if pid is None:
        raise ChatHeaderMismatch(
            "WhatsApp.app is not running; cannot read focused-window header — "
            "start WhatsApp Desktop and retry"
        )

    app = AXUIElementCreateApplication(pid)

    # Retry ONLY while the candidate list is EMPTY — i.e. "the header could
    # not be read", the transient-sheet case above. A NON-empty list that
    # does not match is a genuine wrong-chat condition and still aborts on
    # the first look, so the D-03 wrong-chat guard is unchanged in
    # strength; only the unreadable case is given more time.
    headings: list[str] = []
    err = 0
    window = None
    for attempt in range(_HEADER_SETTLE_ATTEMPTS):
        if attempt:
            time.sleep(_HEADER_SETTLE_INTERVAL_S)
        err, window = AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute, None)
        if err != 0 or window is None:
            continue
        headings = _walk_for_heading(
            window,
            roles=_DEFAULT_HEADING_ROLES,
            identifier=_CHAT_HEADER_IDENTIFIER,
        )
        if headings:
            break

    if err != 0 or window is None:
        raise ChatHeaderMismatch(
            f"AXFocusedWindow lookup failed (err={err}); cannot verify chat header — "
            "bring WhatsApp Desktop to foreground and retry"
        )

    expected = _strip_bidi(expected_chat_name).casefold()

    for h in headings:
        if expected in _strip_bidi(h).casefold():
            logger.debug(
                "assert_focused_chat_matches: matched expected=%r in observed header",
                expected_chat_name,
            )
            return

    raise ChatHeaderMismatch(
        f"Focused chat header does not match expected={expected_chat_name!r}; "
        f"observed chat-header candidates (stripped) = "
        f"{[_strip_bidi(h) for h in headings]}"
    )


def _assert_first_search_result_matches(chat_name: str) -> None:
    """Verify the topmost sidebar-search result matches the expected chat name.

    Companion preflight for Plan 02-03's group-send fallback (CONTEXT.md
    D-02 search-and-click flow). The orchestrator types the chat name
    into the sidebar search field, waits for results to render, and
    THEN calls this function before pressing Return to open the chat —
    so the wrong-chat protection covers the group-fallback path the
    same way :func:`assert_focused_chat_matches` covers the 1:1
    deep-link path.

    Algorithm matches :func:`assert_focused_chat_matches` with two
    differences:

    1. The role filter is widened to ``{"AXHeading", "AXButton"}``
       (SP-5 locked: sidebar result rows are ``AXButton`` with the chat
       display name in ``AXDescription``; the ``AXHeading`` siblings in
       the sidebar — section labels like "Discussions", date separators
       — are harmless because the substring match accommodates them
       cleanly).
    2. The error message references the search-result context, not
       the chat header context, so audit-log readers can distinguish
       which preflight failed.
    """
    if not _PYOBJC_AVAILABLE:
        raise AccessibilityAPIUnavailable(
            "pyobjc not available; cannot perform AX preflight — reinstall "
            "pyobjc-core, pyobjc-framework-Cocoa, and "
            "pyobjc-framework-ApplicationServices to enable wrong-chat protection"
        )

    pid = _resolve_whatsapp_pid()
    if pid is None:
        raise ChatHeaderMismatch(
            "WhatsApp.app is not running; cannot read sidebar search results — "
            "start WhatsApp Desktop and retry"
        )

    app = AXUIElementCreateApplication(pid)
    err, window = AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute, None)
    if err != 0 or window is None:
        raise ChatHeaderMismatch(
            f"AXFocusedWindow lookup failed (err={err}); cannot verify "
            "sidebar search results — bring WhatsApp Desktop to foreground "
            "and retry"
        )

    labels = _walk_for_heading(window, roles=_SIDEBAR_RESULT_ROLES)
    expected = _strip_bidi(chat_name).casefold()

    for label in labels:
        if expected in _strip_bidi(label).casefold():
            logger.debug(
                "_assert_first_search_result_matches: matched expected=%r in sidebar",
                chat_name,
            )
            return

    raise ChatHeaderMismatch(
        f"Sidebar search topmost result does not match expected chat_name="
        f"{chat_name!r}; observed sidebar label(s) (stripped) = "
        f"{[_strip_bidi(label) for label in labels]}"
    )


def _find_by_identifier(elem: Any, identifier: str) -> Any | None:
    """Bounded breadth-first walk; return the first node whose ``AXIdentifier``
    equals ``identifier`` exactly, or ``None`` if the budget runs out.

    Same traversal discipline as :func:`_walk_for_heading` — BFS (``pop(0)``)
    so a deep message-list subtree cannot bury a shallow chrome node, bounded
    at ``_MAX_WALK_NODES``. Returns the element itself, not its labels.
    """
    queue: list[Any] = [elem]
    visited = 0
    while queue and visited < _MAX_WALK_NODES:
        node = queue.pop(0)
        visited += 1

        id_err, node_id = AXUIElementCopyAttributeValue(node, kAXIdentifierAttribute, None)
        if id_err == 0 and node_id == identifier:
            return node

        kids_err, kids = AXUIElementCopyAttributeValue(node, kAXChildrenAttribute, None)
        if kids_err == 0 and kids:
            queue.extend(kids)

    return None


def focus_composer() -> None:
    """Move keyboard focus into the focused chat's message composer.

    Return-selecting a sidebar search result opens the chat but leaves focus
    in the search field, so every keystroke the group send fires afterwards
    lands there instead of the composer — see :class:`ComposerNotFocused`.
    The 1:1 deeplink path does not need this: ``whatsapp://send`` focuses the
    composer itself.

    Fires ``AXPress`` on the ``ChatBar_ComposerTextView`` node, then re-reads
    ``AXFocusedUIElement`` until it reports that same identifier. Raises
    rather than returning a boolean: every caller must abort the send, and a
    silently-ignored False would type the body into the search field.

    Raises:
        AccessibilityAPIUnavailable: pyobjc runtime imports failed (D-06).
        ComposerNotFocused: WhatsApp is not running, the focused window or
            the composer node could not be read, or focus did not land on
            the composer within the settle budget.
    """
    if not _PYOBJC_AVAILABLE:
        raise AccessibilityAPIUnavailable(
            "pyobjc is unavailable; cannot focus the message composer. "
            "Install with: pip install pyobjc-core pyobjc-framework-Cocoa "
            "pyobjc-framework-ApplicationServices"
        )

    pid = _resolve_whatsapp_pid()
    if pid is None:
        raise ComposerNotFocused(
            "WhatsApp.app is not running; cannot focus the message composer — "
            "start WhatsApp Desktop and retry"
        )

    app = AXUIElementCreateApplication(pid)
    err, window = AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute, None)
    if err != 0 or window is None:
        raise ComposerNotFocused(
            f"AXFocusedWindow lookup failed (err={err}); cannot focus the message "
            "composer — bring WhatsApp Desktop to foreground and retry"
        )

    composer = _find_by_identifier(window, _COMPOSER_IDENTIFIER)
    if composer is None:
        raise ComposerNotFocused(
            f"no node with AXIdentifier={_COMPOSER_IDENTIFIER!r} under the focused "
            "window; the chat may not be open, or this WhatsApp build renames the "
            "composer"
        )

    # AXPress, NOT AXFocused. WhatsApp Catalyst reports AXFocused as settable
    # and returns success (rc=0) for the write, then ignores it — verified
    # live on 26.31.23: focus stayed on the search field for a full 2s of
    # polling. AXPress on the same node lands focus in ~0.1s.
    AXUIElementPerformAction(composer, "AXPress")

    for attempt in range(_FOCUS_SETTLE_ATTEMPTS):
        if attempt:
            time.sleep(_FOCUS_SETTLE_INTERVAL_S)
        focus_err, focused = AXUIElementCopyAttributeValue(app, kAXFocusedUIElementAttribute, None)
        if focus_err != 0 or focused is None:
            continue
        id_err, node_id = AXUIElementCopyAttributeValue(focused, kAXIdentifierAttribute, None)
        if id_err == 0 and node_id == _COMPOSER_IDENTIFIER:
            logger.debug("focus_composer: composer focused after %d attempt(s)", attempt + 1)
            return

    raise ComposerNotFocused(
        f"pressed {_COMPOSER_IDENTIFIER!r} but AXFocusedUIElement never "
        f"reported it within {_FOCUS_SETTLE_ATTEMPTS * _FOCUS_SETTLE_INTERVAL_S:.2f}s; "
        "aborting rather than typing into whatever holds focus"
    )
