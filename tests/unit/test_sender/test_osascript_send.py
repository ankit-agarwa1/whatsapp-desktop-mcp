"""Unit tests for ``sender.osascript_send`` — D-01 step 3 / D-02 step 8.

Covers:

* ``press_return`` — clean exit returns None; ``error_code=-1743`` →
  :class:`AutomationRevoked`; any other non-zero → :class:`OsascriptError`.
* ``type_string`` — BMP-only enforcement (non-BMP raises with the
  offending code point in the message); AppleScript double-quote +
  backslash escaping; -1743 mid-typing → AutomationRevoked.

All tests patch :func:`whatsapp_desktop_mcp.sender.osascript_send.run_osascript`
so no real ``/usr/bin/osascript`` subprocess fires.
"""

from __future__ import annotations

import pytest

from whatsapp_desktop_mcp.exceptions import AutomationRevoked, OsascriptError
from whatsapp_desktop_mcp.permissions.osascript import OsascriptResult
from whatsapp_desktop_mcp.sender import osascript_send

# ---------------------------------------------------------------------------
# press_return
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_press_return_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``exit_code == 0`` → returns None silently."""

    async def fake(_script: str, timeout: float = 3.0) -> OsascriptResult:
        return OsascriptResult(exit_code=0, stdout="", stderr="", error_code=None)

    monkeypatch.setattr(osascript_send, "run_osascript", fake)

    # press_return is async def with no explicit return; awaiting it
    # yields None on the success path. We do NOT bind the result to a
    # variable (mypy --strict would flag the implicit-None pattern as
    # ``func-returns-value``); reaching this line at all is the test
    # signal.
    await osascript_send.press_return()


@pytest.mark.asyncio
async def test_press_return_raises_automation_revoked_on_1743(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``error_code=-1743`` (errAEEventNotPermitted) → :class:`AutomationRevoked`."""

    async def fake(_script: str, timeout: float = 3.0) -> OsascriptResult:
        return OsascriptResult(
            exit_code=1,
            stdout="",
            stderr="execution error: Not authorized (-1743)",
            error_code=-1743,
        )

    monkeypatch.setattr(osascript_send, "run_osascript", fake)

    with pytest.raises(AutomationRevoked, match="System Settings"):
        await osascript_send.press_return()


@pytest.mark.asyncio
async def test_press_return_raises_osascript_error_on_other_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any non-zero exit other than ``-1743`` → :class:`OsascriptError`."""

    async def fake(_script: str, timeout: float = 3.0) -> OsascriptResult:
        return OsascriptResult(
            exit_code=1,
            stdout="",
            stderr="execution error: target not found (-1728)",
            error_code=-1728,
        )

    monkeypatch.setattr(osascript_send, "run_osascript", fake)

    with pytest.raises(OsascriptError, match="-1728"):
        await osascript_send.press_return()


# ---------------------------------------------------------------------------
# type_string
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_type_string_rejects_non_bmp(monkeypatch: pytest.MonkeyPatch) -> None:
    """U+1F600 (😀) is rejected up-front; codepoint appears in the message."""

    async def fake(_script: str, timeout: float = 3.0) -> OsascriptResult:
        # Should NEVER be called — rejection happens before the subprocess.
        raise AssertionError("run_osascript should not be invoked for non-BMP input")

    monkeypatch.setattr(osascript_send, "run_osascript", fake)

    with pytest.raises(OsascriptError, match=r"U\+1F600"):
        await osascript_send.type_string("hi 😀")


@pytest.mark.asyncio
async def test_type_string_escapes_double_quote_and_backslash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backslashes are escaped first, then double quotes (order matters)."""
    captured_scripts: list[str] = []

    async def fake(script: str, timeout: float = 3.0) -> OsascriptResult:
        captured_scripts.append(script)
        return OsascriptResult(exit_code=0, stdout="", stderr="", error_code=None)

    monkeypatch.setattr(osascript_send, "run_osascript", fake)

    # The body has a backslash followed by a double quote — the escape
    # order MUST be backslash-first then quote, else the quote escape
    # would produce ``\"`` and the subsequent backslash pass would
    # double-escape the slash to ``\\\\``.
    await osascript_send.type_string('hello \\ "world"')

    assert captured_scripts, "expected one osascript invocation"
    script = captured_scripts[0]
    # Backslash in the body → ``\\`` (escaped to AppleScript double-backslash).
    # Double-quote in the body → ``\"``.
    # The full escaped body inside the script literal:
    assert 'keystroke "hello \\\\ \\"world\\""' in script


@pytest.mark.asyncio
async def test_type_string_clean_exit_with_bmp_unicode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BMP-range characters (``héllo``) succeed cleanly."""

    async def fake(_script: str, timeout: float = 3.0) -> OsascriptResult:
        return OsascriptResult(exit_code=0, stdout="", stderr="", error_code=None)

    monkeypatch.setattr(osascript_send, "run_osascript", fake)

    # é = U+00E9 is well within BMP. Should not raise.
    await osascript_send.type_string("héllo")


@pytest.mark.asyncio
async def test_type_string_raises_automation_revoked_on_1743(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``error_code=-1743`` mid-typing → :class:`AutomationRevoked`."""

    async def fake(_script: str, timeout: float = 3.0) -> OsascriptResult:
        return OsascriptResult(
            exit_code=1,
            stdout="",
            stderr="(-1743)",
            error_code=-1743,
        )

    monkeypatch.setattr(osascript_send, "run_osascript", fake)

    with pytest.raises(AutomationRevoked):
        await osascript_send.type_string("hi")


@pytest.mark.asyncio
async def test_type_string_raises_osascript_error_on_other_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any non-zero exit other than -1743 → :class:`OsascriptError`."""

    async def fake(_script: str, timeout: float = 3.0) -> OsascriptResult:
        return OsascriptResult(
            exit_code=2,
            stdout="",
            stderr="syntax error (-2741)",
            error_code=-2741,
        )

    monkeypatch.setattr(osascript_send, "run_osascript", fake)

    with pytest.raises(OsascriptError, match="-2741"):
        await osascript_send.type_string("hi")


@pytest.mark.asyncio
async def test_type_string_rejects_first_non_bmp_codepoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first non-BMP code point in the body is the one named in the error."""

    async def fake(_script: str, timeout: float = 3.0) -> OsascriptResult:
        raise AssertionError("rejected before subprocess")

    monkeypatch.setattr(osascript_send, "run_osascript", fake)

    # 🎉 = U+1F389 appears before 🚀 = U+1F680 — the message references
    # the first offending code point seen during the linear scan.
    with pytest.raises(OsascriptError, match=r"U\+1F389"):
        await osascript_send.type_string("party 🎉🚀")


# ---------------------------------------------------------------------------
# press_return targets WhatsApp (verified-live regression)
# ---------------------------------------------------------------------------
#
# `System Events` keystrokes go to the FRONTMOST app, but the deep-link is
# spawned with `open -g`, which deliberately leaves WhatsApp in the
# background. Without an activate, the Return landed in whatever app the user
# had in front: the body stayed in WhatsApp's compose box unsent while
# osascript exited 0, so the caller reported `sent_unverified` for a send that
# never happened. Verified live on 26.31.23.


@pytest.mark.asyncio
async def test_press_return_activates_whatsapp_before_keystroking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The activate must precede the keystroke, or the Return goes elsewhere."""
    scripts: list[str] = []

    async def fake(script: str, timeout: float = 3.0) -> OsascriptResult:
        scripts.append(script)
        return OsascriptResult(exit_code=0, stdout="", stderr="", error_code=None)

    monkeypatch.setattr(osascript_send, "run_osascript", fake)
    monkeypatch.setattr(osascript_send, "_ACTIVATE_SETTLE_S", 0)

    await osascript_send.press_return()

    assert len(scripts) == 2, f"expected activate + keystroke, got {scripts}"
    assert 'tell application "WhatsApp" to activate' == scripts[0]
    assert "keystroke return" in scripts[1]


@pytest.mark.asyncio
async def test_press_return_does_not_keystroke_when_activate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed activate must abort — keystroking into the wrong app is the bug."""
    scripts: list[str] = []

    async def fake(script: str, timeout: float = 3.0) -> OsascriptResult:
        scripts.append(script)
        return OsascriptResult(exit_code=1, stdout="", stderr="boom", error_code=None)

    monkeypatch.setattr(osascript_send, "run_osascript", fake)
    monkeypatch.setattr(osascript_send, "_ACTIVATE_SETTLE_S", 0)

    with pytest.raises(OsascriptError, match="activating WhatsApp failed"):
        await osascript_send.press_return()

    assert scripts == ['tell application "WhatsApp" to activate'], (
        "must not fall through to the keystroke when activation failed"
    )


@pytest.mark.asyncio
async def test_press_return_maps_1743_on_activate_to_automation_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-6: a revoked Automation grant surfaces structurally, not as a traceback."""

    async def fake(_script: str, timeout: float = 3.0) -> OsascriptResult:
        return OsascriptResult(exit_code=1, stdout="", stderr="", error_code=-1743)

    monkeypatch.setattr(osascript_send, "run_osascript", fake)
    monkeypatch.setattr(osascript_send, "_ACTIVATE_SETTLE_S", 0)

    with pytest.raises(AutomationRevoked, match="before activate"):
        await osascript_send.press_return()
