"""``@timeout`` decorator tests — REL-03 + Plan 04 _decorators.py.

The decorator must:
- Pass through normal returns when the wrapped function finishes under budget.
- Convert ``TimeoutError`` -> structured ``ValueError`` (FastMCP-friendly).
- Preserve ``functools.wraps`` invariants (``__name__``, ``__wrapped__``).
- Restart its budget on ``restart_timeout()`` so wall-clock spent awaiting a
  human (``send_message``'s elicitation) is not charged to the machine steps.
"""

from __future__ import annotations

import asyncio

import pytest

from whatsapp_desktop_mcp.tools._decorators import restart_timeout, suspend_timeout, timeout


@pytest.mark.asyncio
async def test_timeout_returns_value_when_under_budget() -> None:
    """A function that returns under budget passes its value through."""

    @timeout(seconds=1.0)
    async def fast() -> int:
        return 42

    assert await fast() == 42


@pytest.mark.asyncio
async def test_timeout_raises_value_error_on_overrun() -> None:
    """A function exceeding the budget raises ``ValueError`` (NOT raw TimeoutError)."""

    @timeout(seconds=0.01)
    async def slow() -> int:
        await asyncio.sleep(1.0)
        return 0

    with pytest.raises(ValueError) as exc_info:
        await slow()
    # The error must mention the timeout budget so the LLM gets useful signal.
    assert "0.01" in str(exc_info.value) or "timeout" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_timeout_preserves_function_signature() -> None:
    """``functools.wraps`` invariants survive: ``__name__`` and ``__wrapped__``."""

    @timeout(seconds=1.0)
    async def my_named_function(a: int, b: int) -> int:
        return a + b

    assert my_named_function.__name__ == "my_named_function"
    # functools.wraps sets __wrapped__ to the original function.
    assert hasattr(my_named_function, "__wrapped__")


@pytest.mark.asyncio
async def test_timeout_propagates_other_exceptions() -> None:
    """Non-TimeoutError exceptions from the wrapped function propagate unchanged."""

    @timeout(seconds=1.0)
    async def boom() -> int:
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        await boom()


@pytest.mark.asyncio
async def test_restart_timeout_gives_the_machine_steps_a_fresh_budget() -> None:
    """REL-03: ``restart_timeout()`` restarts the budget from now.

    Regression for the long-body ``send_message`` failure: a ~900-char body
    made the human turn (``ctx.elicit``, which renders the body verbatim) long
    enough that it plus the ~10 s machine phase exceeded the 15 s envelope, so
    the tool aborted with "Tool exceeded 15s timeout." having sent nothing.

    Here the two 0.15 s sleeps stand in for the human turn and the machine
    steps after it. Their sum (0.30 s) exceeds the 0.2 s budget, so this call
    raises ValueError unless the budget restarts between them.
    """

    @timeout(seconds=0.2)
    async def confirm_then_send() -> int:
        await asyncio.sleep(0.15)  # STEP 6 — the human reads a long body
        restart_timeout()
        await asyncio.sleep(0.15)  # STEPS 7-11 — deeplink + keystroke + verify
        return 42

    assert await confirm_then_send() == 42


@pytest.mark.asyncio
async def test_restart_timeout_does_not_disable_the_budget() -> None:
    """A restart pushes the deadline out by one budget — it does not remove it."""

    @timeout(seconds=0.05)
    async def still_hangs() -> int:
        restart_timeout()
        await asyncio.sleep(1.0)
        return 0

    with pytest.raises(ValueError, match="timeout"):
        await still_hangs()


@pytest.mark.asyncio
async def test_restart_timeout_outside_a_timeout_scope_is_a_noop() -> None:
    """Called with no enclosing ``@timeout`` it must not raise."""
    restart_timeout()


# ---------------------------------------------------------------------------
# suspend_timeout — the human turn must not be able to kill the tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_after_the_await_alone_cannot_survive_a_long_human_turn() -> None:
    """REGRESSION: restarting only AFTER the await leaves the real bug open.

    The scope fires while the task is suspended at the elicit, so the restart
    line is never reached. This is exactly the field failure: the tool died at
    ``ctx.elicit`` having sent nothing.
    """

    @timeout(seconds=0.2)
    async def body() -> str:
        await asyncio.sleep(0.3)  # human turn alone outlasts the budget
        restart_timeout()  # never reached
        return "sent"

    with pytest.raises(ValueError, match="0.2s"):
        await body()


@pytest.mark.asyncio
async def test_suspend_then_restart_survives_a_human_turn_longer_than_the_budget() -> None:
    """Suspending for the human turn is what actually closes the gap."""

    @timeout(seconds=0.2)
    async def body() -> str:
        suspend_timeout()
        await asyncio.sleep(0.3)  # human turn, longer than the whole budget
        restart_timeout()
        await asyncio.sleep(0.05)  # STEPS 7-11, machine work
        return "sent"

    assert await body() == "sent"


@pytest.mark.asyncio
async def test_suspend_does_not_leave_the_budget_disabled_after_restart() -> None:
    """A suspended-then-restarted scope must still catch a machine hang."""

    @timeout(seconds=0.2)
    async def body() -> str:
        suspend_timeout()
        await asyncio.sleep(0.05)
        restart_timeout()
        await asyncio.sleep(1.0)  # wedged machine step
        return "sent"

    with pytest.raises(ValueError, match="0.2s"):
        await body()


@pytest.mark.asyncio
async def test_suspend_timeout_outside_a_timeout_scope_is_a_noop() -> None:
    """Read tools call neither helper; the module-level default must be safe."""
    suspend_timeout()  # must not raise
