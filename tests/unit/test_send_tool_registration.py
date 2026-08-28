"""Regression gate: ``--no-read-only`` must actually ADVERTISE ``send_message``.

Why this file exists
--------------------
Upstream shipped a dead send feature. ``server.py`` evaluates its
``if not read_only_mode:`` registration gate AT IMPORT TIME, but ``cli.main()``
imported the server module *first* and assigned ``server.read_only_mode``
*after*. The gate had therefore already been evaluated against the default
(``True``), so ``send_message`` was never registered and ``--no-read-only``
silently produced a read-only server.

The pre-existing tests did not catch it because they only assert that the
*attribute* is assignable (``test_read_only_mode.py``) and that
``--no-read-only --help`` exits 0. Neither observes the actual tool list.

The only test that catches this class of bug is one that spawns the real
process and reads ``tools/list`` off the wire, which is what this does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_HANDSHAKE_TIMEOUT_S = 60


def _tool_names(*flags: str) -> set[str]:
    """Spawn the server over stdio and return the advertised tool names."""
    env = dict(os.environ)
    # Do NOT let an ambient value decide the outcome — the flag must win.
    env.pop("WHATSAPP_DESKTOP_MCP_READ_ONLY", None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "whatsapp_desktop_mcp", *flags],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=env,
    )
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None

        def send(payload: dict[str, object]) -> None:
            proc.stdin.write(json.dumps(payload) + "\n")  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "regression-probe", "version": "0"},
                },
            }
        )
        proc.stdout.readline()  # initialize result
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = json.loads(proc.stdout.readline())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=_HANDSHAKE_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()

    return {tool["name"] for tool in listed["result"]["tools"]}


def test_default_is_read_only_and_hides_send_message() -> None:
    """No flags -> 8 read tools, and send_message is NOT advertised."""
    names = _tool_names()
    assert "send_message" not in names, (
        f"default server must not advertise send_message; got {sorted(names)}"
    )
    assert len(names) == 8, f"expected 8 read tools, got {len(names)}: {sorted(names)}"


def test_no_read_only_registers_send_message() -> None:
    """``--no-read-only`` -> send_message IS advertised (the upstream bug)."""
    names = _tool_names("--no-read-only")
    assert "send_message" in names, (
        "--no-read-only must register send_message; the import-ordering bug makes "
        f"this silently fail. Got {sorted(names)}"
    )
    assert len(names) == 9, f"expected 9 tools, got {len(names)}: {sorted(names)}"


@pytest.mark.parametrize("flag", ["--read-only", "--no-read-only"])
def test_flag_beats_ambient_env_var(flag: str) -> None:
    """The CLI flag is authoritative over an inherited env var."""
    names = _tool_names(flag)
    assert ("send_message" in names) is (flag == "--no-read-only")
