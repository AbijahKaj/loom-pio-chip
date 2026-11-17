#!/bin/sh
# Local Tiny Tapeout hardening (LibreLane in Docker). Run from the repo root.
# Requires: Docker running, venv at $TTVENV with tt/requirements.txt + librelane installed.
TTVENV=${TTVENV:-/Users/admin/Hardware/ttsetup/venv}
. "$TTVENV/bin/activate"
export PDK_ROOT=${PDK_ROOT:-/Users/admin/Hardware/ttsetup/pdk}
export PDK=ihp-sg13cmos5l
export LIBRELANE_TAG=${LIBRELANE_TAG:-3.1.0.dev4}
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
python ./tt/tt_tool.py --pdk $PDK --create-user-config || exit 1
python ./tt/tt_tool.py --pdk $PDK --harden || exit 1
python ./tt/tt_tool.py --pdk $PDK --print-warnings
python ./tt/tt_tool.py --pdk $PDK --print-stats 2>/dev/null
python ./tt/tt_tool.py --pdk $PDK --print-cell-category 2>/dev/null | tail -30
