"""``@timeout(seconds=N)`` decorator — wraps an async tool body in ``asyncio.wait_for``.

REL-03 mandates per-tool timeouts at the tool layer (NOT the reader layer). Plan
01-04 applies this decorator to each read-tool body so a stuck SQLite call,
runaway LIKE scan, or a ``database is locked`` storm cannot hang the stdio loop
indefinitely. Reader-level ``PRAGMA busy_timeout = 5000`` handles transient
write contention; the tool-level timeout is the outer envelope.

Design choices:

- The decorator factory ``timeout(seconds)`` returns a decorator that wraps an
  async callable in ``asyncio.timeout``. The wrapped body runs to completion
  or raises a Python 3.11+ ``TimeoutError`` (which is an alias of
  ``asyncio.TimeoutError`` since 3.11; ruff UP041 enforces the alias-free
  spelling).
- ``restart_timeout()`` restarts that budget from "now". The budget exists to
  stop a MACHINE hang (see above); it was never a limit on how long a HUMAN may
  take. ``send_message`` awaits an MCP elicitation — a human reading the
  verbatim body and clicking confirm — inside its envelope, and a long body is
  a long read, so the human turn alone could exhaust the budget before a single
  byte was sent. The tool body calls ``restart_timeout()`` the moment that human
  turn ends, so the machine steps that follow get the whole budget. Implemented
  with ``asyncio.timeout`` (not ``wait_for``) purely because ``wait_for`` gives
  no handle on its deadline and ``asyncio.Timeout.reschedule`` does.
- On timeout we re-raise as a plain ``ValueError`` rather than letting the
  ``TimeoutError`` escape — FastMCP converts ``ValueError`` into a structured
  ``tools/call`` error response that the LLM sees; an unhandled
  ``TimeoutError`` would surface as a Python traceback string with less
  signal. This matches the RESEARCH §"Pattern 2 → @timeout decorator block"
  prescription verbatim.
- ``functools.wraps`` preserves the decorated function's name + signature so
  FastMCP's introspection (which builds the JSON-schema for tool inputs from
  the wrapped callable's annotations) sees the original signature, not the
  wrapper's ``*args, **kwargs``.
- Typed with ``ParamSpec("P")`` + ``TypeVar("R")`` so mypy --strict can verify
  the decorator preserves the input/output signature of the decorated
  function. Without ParamSpec the wrapper would be typed
  ``Callable[..., Awaitable[R]]`` and we'd lose argument-shape inference.

The decorator is deliberately tiny and side-effect-free at import time so
``tools/_decorators.py`` is safe to import from every tool module without
ordering concerns.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from functools import wraps
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

# Set by the innermost active ``@timeout`` scope to a nullary callable that
# pushes that scope's deadline out by its full budget. A ContextVar (not a
# module global) because the stdio server drives every tool call on one event
# loop: each asyncio task carries its own copy, so two concurrent tool calls
# cannot reschedule each other's deadline.
_restart_active_timeout: ContextVar[Callable[[], None] | None] = ContextVar(
    "_restart_active_timeout", default=None
)

# Companion to the above: drops the deadline entirely for the duration of a
# non-machine await. Separate ContextVar rather than a tuple so the existing
# restart-only contract and its tests are untouched.
_suspend_active_timeout: ContextVar[Callable[[], None] | None] = ContextVar(
    "_suspend_active_timeout", default=None
)


def restart_timeout() -> None:
    """Restart the enclosing ``@timeout`` budget from now; no-op outside one.

    Call this after awaiting something that is NOT machine work — today that
    means exactly one caller, ``send_message``'s ``ctx.elicit`` confirmation
    prompt. The wall-clock a human spends reading a long message must not be
    charged against a budget that exists to catch a wedged osascript or a
    ``database is locked`` storm.
    """
    restart = _restart_active_timeout.get()
    if restart is not None:
        restart()


def suspend_timeout() -> None:
    """Remove the enclosing ``@timeout`` deadline; no-op outside one.

    Call this IMMEDIATELY BEFORE awaiting something that is not machine work,
    and :func:`restart_timeout` immediately after. Restarting afterwards is
    not sufficient on its own: the scope fires while the task is suspended at
    the await, so a human turn that alone outlasts the budget kills the tool
    before the restart line is ever reached.
    """
    suspend = _suspend_active_timeout.get()
    if suspend is not None:
        suspend()


def timeout(seconds: float) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Wrap an async tool body in ``asyncio.timeout(seconds)``.

    Usage::

        @mcp.tool(name="read_chat", annotations=..., meta=...)
        @timeout(seconds=5)
        async def read_chat(chat_id: int, ...) -> dict:
            ...

    ORDERING NOTE: ``@mcp.tool`` is applied first (source-order, outermost),
    ``@timeout`` second (innermost). This means ``@timeout`` is the wrapper
    closest to the function body — FastMCP registers the timeout-wrapped
    callable as the tool. If the order were reversed, FastMCP would register
    the raw body and the timeout would never apply.

    Args:
        seconds: Wall-clock budget for MACHINE work. When exceeded the
            wrapper raises :class:`ValueError` carrying the budget in its
            message — FastMCP converts that into a structured ``tools/call``
            error response. A body that awaits a human mid-flight calls
            :func:`restart_timeout` afterwards to restart this budget.
    """

    def deco(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(fn)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                async with asyncio.timeout(seconds) as scope:

                    def restart() -> None:
                        scope.reschedule(asyncio.get_running_loop().time() + seconds)

                    def suspend() -> None:
                        # ``when=None`` disables the deadline; the paired
                        # restart() re-arms it with a full fresh budget.
                        scope.reschedule(None)

                    token = _restart_active_timeout.set(restart)
                    suspend_token = _suspend_active_timeout.set(suspend)
                    try:
                        return await fn(*args, **kwargs)
                    finally:
                        _restart_active_timeout.reset(token)
                        _suspend_active_timeout.reset(suspend_token)
            except TimeoutError as exc:
                # Surface as a structured MCP error, not a Python traceback.
                # The MCP framework converts ValueError into a tool error
                # response; an unhandled TimeoutError would surface as a
                # less-helpful Python traceback string.
                raise ValueError(
                    f"Tool exceeded {seconds}s timeout. The WhatsApp DB may "
                    f"be under heavy write load — retry in a moment, or "
                    f"narrow the query."
                ) from exc

        return inner

    return deco
