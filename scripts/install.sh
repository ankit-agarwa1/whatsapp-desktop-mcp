#!/usr/bin/env bash
# Install whatsapp-desktop-mcp into a fixed-path virtualenv.
#
# WHY THIS EXISTS (and why there is no Homebrew formula):
#
#   1. macOS TCC keys permission grants by the ABSOLUTE PATH of the binary that
#      asks. Full Disk Access / Accessibility must be granted to the Python
#      interpreter running the server. An installer whose path moves on upgrade
#      (uvx, `uv tool upgrade`) silently drops those grants and the server stops
#      being able to read WhatsApp's database. This script pins the venv to one
#      path that never changes.
#
#   2. The venv is created with `--copies`, NOT the default symlinks. A symlinked
#      venv python resolves to the shared Homebrew/system interpreter, so
#      granting it Full Disk Access grants FDA to EVERY python on the machine
#      that resolves to the same binary. `--copies` gives this tool its own real
#      binary, so the grant is scoped to this server alone.
#
#   3. Homebrew installs Python packages with `--no-binary`, forcing source
#      builds. pyobjc-framework-Quartz does not compile on macOS 15+
#      (CGWindowListCreateImageFromArray was obsoleted in 15.0 and pyobjc builds
#      with -Werror). pip here uses wheels, which work fine.
#
# Usage:
#   ./scripts/install.sh              # read-only server (default; compiles nothing)
#   ./scripts/install.sh --with-send  # adds pyobjc for the AX send preflight
set -euo pipefail

PREFIX="${WHATSAPP_MCP_PREFIX:-$HOME/.local/share/whatsapp-desktop-mcp}"
VENV="$PREFIX/venv"
BIN="$VENV/bin/whatsapp-desktop-mcp"
WITH_SEND=0
[[ "${1:-}" == "--with-send" ]] && WITH_SEND=1

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: macOS only (the reader uses WhatsApp's Core Data store; the sender uses Apple Events)." >&2
  exit 1
fi

PY=""
for c in python3.13 python3.12 /opt/homebrew/opt/python@3.12/bin/python3.12 /usr/local/opt/python@3.12/bin/python3.12; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,12) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [[ -z "$PY" ]]; then
  echo "error: need Python 3.12+. Install with: brew install python@3.12" >&2
  exit 1
fi
echo "==> interpreter: $PY ($("$PY" --version))"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> creating venv (--copies, for a private TCC identity): $VENV"
rm -rf "$VENV"; mkdir -p "$PREFIX"
"$PY" -m venv --copies "$VENV"

echo "==> installing (wheels only for native deps; never --no-binary)"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
if [[ "$WITH_SEND" == "1" ]]; then
  "$VENV/bin/python" -m pip install --quiet "$REPO[send]"
else
  "$VENV/bin/python" -m pip install --quiet "$REPO"
fi

chmod 700 "$PREFIX"
echo
echo "==> installed: $BIN"
"$BIN" --version
cat <<MSG

Next steps
----------
1. Grant macOS permissions to EXACTLY this path:

     $VENV/bin/python

   - Full Disk Access (required, to read WhatsApp's local database)
       x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles
   - Accessibility (only needed with --with-send)
       x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility

2. Register with Claude Code:

     claude mcp add --scope user whatsapp-desktop -- $BIN

3. Verify by asking Claude to call the 'doctor' tool. It reports which
   permission buckets are still missing and the exact path to grant.

The server starts READ-ONLY by default. Pass --no-read-only to enable sending
(requires ./scripts/install.sh --with-send).
MSG
