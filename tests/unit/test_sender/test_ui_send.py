"""Unit tests for ``sender.ui_send`` — D-01 / D-02 / D-03 load-bearing P5.

Covers:

* ``send_text(kind="direct")`` dispatches to deeplink + AX preflight +
  press_return in EXACTLY that source-order (the D-03 invariant).
* ``send_text(kind="direct")`` requires ``recipient_phone_e164``.
* ``send_text(kind="direct")`` returns ``is_experimental=False``.
* ``send_text(kind="group")`` runs the search-and-click fallback and
  returns ``is_experimental=True``.
* ``send_text(kind="other")`` → :class:`NotImplementedError`.
* ``send_group_via_search`` calls ``_assert_first_search_result_matches``
  BEFORE the first ``press_return`` on the group path (SP-5 lock).
* ``send_text`` returns ``send_started_unix`` from BEFORE the subprocess
  fires (so the post-hoc verify ``ZMESSAGEDATE > ?`` predicate
  cleanly excludes pre-existing identical bodies).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from whatsapp_desktop_mcp.exceptions import ChatHeaderMismatch
from whatsapp_desktop_mcp.sender import ui_send

# ---------------------------------------------------------------------------
# Call-order recorder
# ---------------------------------------------------------------------------


def _install_call_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch every callable ``send_text`` invokes; return a shared call log.

    The returned list captures the NAME of each call in invocation order,
    enabling assertions like ``call_log == ["send_deeplink",
    "assert_focused_chat_matches", "press_return"]`` — the canonical
    D-03 source-order invariant on the 1:1 direct path.
    """
    call_log: list[str] = []

    async def fake_send_deeplink(phone: str, body: str) -> None:
        call_log.append("send_deeplink")

    def fake_ax_focused(name: str) -> None:
        call_log.append("assert_focused_chat_matches")

    def fake_ax_first_search(name: str) -> None:
        call_log.append("_assert_first_search_result_matches")

    def fake_focus_composer() -> None:
        call_log.append("focus_composer")

    async def fake_press_return(timeout: float = 3.0) -> None:
        call_log.append("press_return")

    async def fake_type_string(text: str, timeout: float = 3.0) -> None:
        call_log.append("type_string")

    async def fake_run_osascript(*_args: object, **_kwargs: object) -> object:
        call_log.append("run_osascript")
        # Mimic the OsascriptResult shape minimally for the group fallback's
        # activate / Cmd-F calls (caller doesn't inspect the result).
        from whatsapp_desktop_mcp.permissions.osascript import OsascriptResult

        return OsascriptResult(exit_code=0, stdout="", stderr="", error_code=None)

    monkeypatch.setattr(ui_send, "send_deeplink", fake_send_deeplink)
    monkeypatch.setattr(ui_send, "assert_focused_chat_matches", fake_ax_focused)
    monkeypatch.setattr(ui_send, "_assert_first_search_result_matches", fake_ax_first_search)
    monkeypatch.setattr(ui_send, "focus_composer", fake_focus_composer)
    monkeypatch.setattr(ui_send, "press_return", fake_press_return)
    monkeypatch.setattr(ui_send, "type_string", fake_type_string)
    monkeypatch.setattr(ui_send, "run_osascript", fake_run_osascript)

    return call_log


# ---------------------------------------------------------------------------
# Direct (1:1) — D-03 source-order invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_text_kind_direct_calls_deeplink_then_ax_then_press_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOAD-BEARING D-03: AX preflight fires BEFORE press_return."""
    call_log = _install_call_log(monkeypatch)

    is_experimental, _send_started = await ui_send.send_text(
        chat_id=42,
        body="hello",
        chat_name="Alice",
        recipient_phone_e164="33612345678",
        kind="direct",
    )

    assert is_experimental is False
    # The structural invariant: send_deeplink → AX preflight → press_return.
    assert call_log == [
        "send_deeplink",
        "assert_focused_chat_matches",
        "press_return",
    ]


@pytest.mark.asyncio
async def test_send_text_kind_direct_returns_send_started_unix_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``send_started_unix`` is captured BEFORE the first subprocess fires."""
    call_log = _install_call_log(monkeypatch)

    before = time.time()
    _, send_started = await ui_send.send_text(
        chat_id=42,
        body="hi",
        chat_name="Alice",
        recipient_phone_e164="33612345678",
        kind="direct",
    )
    after = time.time()

    # The returned send_started_unix is between "before" and "after"
    # (the test's measured bounds). The send_started is captured INSIDE
    # send_text before the first await — so it's ≤ "after" by design.
    assert before <= send_started <= after
    assert call_log  # subprocesses were invoked


@pytest.mark.asyncio
async def test_send_text_direct_with_no_phone_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``kind="direct"`` + ``recipient_phone_e164=None`` → ValueError."""
    _install_call_log(monkeypatch)

    with pytest.raises(ValueError, match="phone_e164"):
        await ui_send.send_text(
            chat_id=42,
            body="hi",
            chat_name="Alice",
            recipient_phone_e164=None,
            kind="direct",
        )


@pytest.mark.asyncio
async def test_send_text_direct_aborts_on_chat_header_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``assert_focused_chat_matches`` raises, ``press_return`` MUST NOT fire.

    Behavioral assertion of the D-03 / SEND-04 P5 mitigation at the
    ui_send tier — the AX preflight is the load-bearing check; if it
    raises ChatHeaderMismatch, no keystroke is dispatched.
    """
    call_log: list[str] = []

    async def fake_send_deeplink(phone: str, body: str) -> None:
        call_log.append("send_deeplink")

    def fake_ax_focused(name: str) -> None:
        call_log.append("assert_focused_chat_matches")
        raise ChatHeaderMismatch("expected=Alice observed=Bob")

    async def fake_press_return(timeout: float = 3.0) -> None:
        call_log.append("press_return")

    monkeypatch.setattr(ui_send, "send_deeplink", fake_send_deeplink)
    monkeypatch.setattr(ui_send, "assert_focused_chat_matches", fake_ax_focused)
    monkeypatch.setattr(ui_send, "press_return", fake_press_return)

    with pytest.raises(ChatHeaderMismatch):
        await ui_send.send_text(
            chat_id=42,
            body="hi",
            chat_name="Alice",
            recipient_phone_e164="33612345678",
            kind="direct",
        )

    # press_return must NOT appear in the call log — AX preflight aborted first.
    assert "press_return" not in call_log
    assert call_log == ["send_deeplink", "assert_focused_chat_matches"]


# ---------------------------------------------------------------------------
# Group fallback — search-and-click flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_text_kind_group_returns_is_experimental_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group sends carry the D-02 fragility flag: ``is_experimental=True``."""
    _install_call_log(monkeypatch)

    is_experimental, _ = await ui_send.send_text(
        chat_id=42,
        body="hi team",
        chat_name="Weekend Plans",
        recipient_phone_e164=None,  # group sends ignore phone
        kind="group",
    )

    assert is_experimental is True


@pytest.mark.asyncio
async def test_send_text_kind_group_calls_search_and_click_with_ax_before_keystroke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group fallback: AX preflight precedes EVERY keystroke on the search path.

    Per the SP-5 / D-03 source-order invariant on the group fallback:

    1. run_osascript (activate)
    2. run_osascript (Cmd-F sidebar focus)
    3. type_string (chat name)
    4. _assert_first_search_result_matches (preflight on topmost result)
    5. press_return (open chat)
    6. assert_focused_chat_matches (preflight on now-focused chat)
    6b. focus_composer (move focus off the sidebar search field)
    7. type_string (body)
    8. press_return (send)

    Both AX preflights (steps 4 + 6) MUST appear BEFORE their respective
    press_return calls (steps 5 + 8), and focus_composer MUST appear
    before the body type_string — otherwise the body is typed into the
    search field and nothing is sent.
    """
    call_log = _install_call_log(monkeypatch)

    await ui_send.send_text(
        chat_id=42,
        body="hello team",
        chat_name="Weekend Plans",
        recipient_phone_e164=None,
        kind="group",
    )

    # Validate the precise step order. Each AX preflight precedes its
    # respective press_return on the same code path.
    idx_first_ax = call_log.index("_assert_first_search_result_matches")
    idx_first_press = call_log.index("press_return")
    assert idx_first_ax < idx_first_press

    idx_focused_ax = call_log.index("assert_focused_chat_matches")
    # The second press_return appears after the focused-chat preflight.
    second_press_idx = call_log.index("press_return", idx_first_press + 1)
    assert idx_focused_ax < second_press_idx

    # Focus is moved off the sidebar search field after the chat opens and
    # BEFORE the body is typed. Without this the body lands in the search
    # field and no message is sent — the live failure this ordering pins.
    idx_focus = call_log.index("focus_composer")
    idx_body_type = call_log.index("type_string", idx_first_press)
    assert idx_focused_ax < idx_focus < idx_body_type


@pytest.mark.asyncio
async def test_send_group_via_search_AX_preflight_on_search_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``_assert_first_search_result_matches`` raises, NO press_return fires."""
    call_log: list[str] = []

    async def fake_run_osascript(*_args: object, **_kwargs: object) -> object:
        call_log.append("run_osascript")
        from whatsapp_desktop_mcp.permissions.osascript import OsascriptResult

        return OsascriptResult(exit_code=0, stdout="", stderr="", error_code=None)

    async def fake_type_string(text: str, timeout: float = 3.0) -> None:
        call_log.append("type_string")

    def fake_ax_first_search(name: str) -> None:
        call_log.append("_assert_first_search_result_matches")
        raise ChatHeaderMismatch("topmost result mismatch")

    def fake_ax_focused(name: str) -> None:
        call_log.append("assert_focused_chat_matches")

    async def fake_press_return(timeout: float = 3.0) -> None:
        call_log.append("press_return")

    monkeypatch.setattr(ui_send, "run_osascript", fake_run_osascript)
    monkeypatch.setattr(ui_send, "type_string", fake_type_string)
    monkeypatch.setattr(ui_send, "_assert_first_search_result_matches", fake_ax_first_search)
    monkeypatch.setattr(ui_send, "assert_focused_chat_matches", fake_ax_focused)
    monkeypatch.setattr(ui_send, "press_return", fake_press_return)

    with pytest.raises(ChatHeaderMismatch):
        await ui_send.send_group_via_search("Weekend Plans", "hi team")

    # press_return must NOT appear — the first AX preflight aborted.
    assert "press_return" not in call_log


# ---------------------------------------------------------------------------
# Unsupported kinds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_text_unsupported_kind_raises_not_implemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``kind="broadcast"`` / ``"community"`` / ``"other"`` → NotImplementedError."""
    _install_call_log(monkeypatch)

    for unsupported in ("broadcast", "community", "other"):
        with pytest.raises(NotImplementedError, match="direct"):
            await ui_send.send_text(
                chat_id=42,
                body="hi",
                chat_name="N",
                recipient_phone_e164=None,
                kind=unsupported,
            )


# ---------------------------------------------------------------------------
# send_image — pasteboard route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_image_asserts_chat_before_pasting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The D-03 assert must precede the paste.

    It cannot sit immediately before the Return the way the text path's does:
    pasting raises WhatsApp's media-preview sheet and AXFocusedWindow then
    resolves to a sheet carrying no chat header. So the ordering pinned here
    IS the guarantee — assert, then paste, then send.
    """
    order: list[str] = []

    def fake_clipboard(image_path: str) -> Path:
        order.append("clipboard")
        return Path(image_path)

    monkeypatch.setattr(ui_send, "put_image_on_clipboard", fake_clipboard)

    async def fake_deeplink(_phone: str, _body: str) -> None:
        order.append("deeplink")

    monkeypatch.setattr(ui_send, "send_deeplink", fake_deeplink)
    monkeypatch.setattr(
        ui_send, "assert_focused_chat_matches", lambda _n: order.append("ax_assert")
    )

    async def fake_paste() -> None:
        order.append("paste")

    async def fake_return() -> None:
        order.append("return")

    monkeypatch.setattr(ui_send, "press_paste", fake_paste)
    monkeypatch.setattr(ui_send, "press_return", fake_return)
    monkeypatch.setattr(ui_send, "_POST_PASTE_SETTLE_S", 0)

    is_experimental, _started = await ui_send.send_image(
        1, str(tmp_path / "x.png"), "Nitin Stable Money", "919398920994", "direct"
    )

    assert is_experimental is False
    assert order == ["clipboard", "deeplink", "ax_assert", "paste", "return"], order


@pytest.mark.asyncio
async def test_send_image_loads_the_file_before_driving_any_ui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bad path must fail with nothing opened and the clipboard untouched."""
    touched: list[str] = []

    async def fake_deeplink(_phone: str, _body: str) -> None:
        touched.append("deeplink")

    monkeypatch.setattr(ui_send, "send_deeplink", fake_deeplink)

    with pytest.raises(FileNotFoundError):
        await ui_send.send_image(
            1, str(tmp_path / "missing.png"), "Nitin Stable Money", "919398920994", "direct"
        )

    assert touched == [], "no UI may be driven when the image cannot be loaded"


@pytest.mark.asyncio
async def test_send_image_rejects_unsupported_chat_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ui_send, "put_image_on_clipboard", lambda p: Path(p))

    with pytest.raises(NotImplementedError, match="does not support chat kind"):
        await ui_send.send_image(1, str(tmp_path / "x.png"), "Somebody", None, "broadcast")


@pytest.mark.asyncio
async def test_send_image_to_group_focuses_composer_before_pasting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Group image send: Cmd-V must not fire while the sidebar search holds focus.

    Verified-live regression. The chat opened and the clipboard held the
    image, but focus was still TokenizedSearchBar_TextView, so Cmd-V went to
    the search field and no message was sent (outcome ``sent_unverified``,
    no ZWAMESSAGE row).
    """
    order = _install_call_log(monkeypatch)

    def fake_clipboard(image_path: str) -> Path:
        order.append("clipboard")
        return Path(image_path)

    monkeypatch.setattr(ui_send, "put_image_on_clipboard", fake_clipboard)

    async def fake_paste() -> None:
        order.append("press_paste")

    monkeypatch.setattr(ui_send, "press_paste", fake_paste)
    monkeypatch.setattr(ui_send, "_POST_PASTE_SETTLE_S", 0)

    is_experimental, _started = await ui_send.send_image(
        516, str(tmp_path / "x.png"), "reminder", None, "group"
    )

    assert is_experimental is True
    assert order.index("focus_composer") < order.index("press_paste")
