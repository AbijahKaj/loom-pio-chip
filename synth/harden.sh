#!/bin/sh
# Local Tiny Tapeout hardening (LibreLane). Run from the repo root.
#
# One-time setup (Linux or macOS), see docs/hardening.md:
#   git clone https://github.com/TinyTapeout/tt-support-tools tt
#   python3 -m venv ../ttsetup/venv && . ../ttsetup/venv/bin/activate
#   pip install -r tt/requirements.txt librelane==3.1.0.dev4
# LibreLane runs its tools through Docker (default) or Nix.
set -e
TTVENV=${TTVENV:-$(cd .. && pwd)/ttsetup/venv}
. "$TTVENV/bin/activate"
export PDK_ROOT=${PDK_ROOT:-$(cd .. && pwd)/ttsetup/pdk}
export PDK=ihp-sg13cmos5l
export LIBRELANE_TAG=${LIBRELANE_TAG:-3.1.0.dev4}
if [ "$(uname)" = "Darwin" ]; then
  # cairo from Homebrew; call python directly so SIP does not strip DYLD_* via /usr/bin/env
  export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
fi
python ./tt/tt_tool.py --pdk $PDK --create-user-config
python ./tt/tt_tool.py --pdk $PDK --harden
python ./tt/tt_tool.py --pdk $PDK --print-warnings || true
python ./tt/tt_tool.py --pdk $PDK --print-stats 2>/dev/null || true
