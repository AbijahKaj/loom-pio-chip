#!/bin/sh
# Harden Loom on a remote Linux host that has Nix (no Docker), without any git remote.
#   synth/remote_harden.sh start   ship the repo (git bundle over SSH), set up, start hardening in the background
#   synth/remote_harden.sh status  show progress
#   synth/remote_harden.sh fetch   copy the results back into ./runs/wokwi (final/, reports, logs)
# Everything on the remote side lives in ~/$RDIR. Run from the repo root.
set -e
HOST=${HOST:-admin-ml@admin-ml}
RDIR=${RDIR:-loom-harden}
LL_TAG=${LL_TAG:-3.1.0.dev4}                 # LibreLane version used by the TT IHP flow
TT_REV=$(git -C tt rev-parse HEAD 2>/dev/null || echo main)

case "${1:-start}" in
start)
  mkdir -p build
  git bundle create build/loom.bundle --all >/dev/null 2>&1
  ssh "$HOST" "mkdir -p ~/$RDIR"
  scp -q build/loom.bundle "$HOST:$RDIR/loom.bundle"
  ssh "$HOST" "RDIR=$RDIR LL_TAG=$LL_TAG TT_REV=$TT_REV bash -s" <<'EOF'
set -e
cd ~/$RDIR
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh 2>/dev/null || true
command -v nix-shell >/dev/null || { echo "nix-shell not found: install Nix first"; exit 1; }
if [ -d loom ]; then git -C loom fetch -q ../loom.bundle main && git -C loom reset -q --hard FETCH_HEAD
else git clone -q loom.bundle loom; fi
[ -d loom/tt ] || git clone -q https://github.com/TinyTapeout/tt-support-tools loom/tt
git -C loom/tt fetch -q origin && git -C loom/tt checkout -q "$TT_REV"
[ -d librelane ] || git clone -q --branch "$LL_TAG" https://github.com/librelane/librelane librelane
cat > run.sh <<'RUN'
#!/bin/sh
set -e
cd "$(dirname "$0")"
export PDK_ROOT=$PWD/pdk PDK=ihp-sg13cmos5l
nix-shell librelane/shell.nix --run '
  set -e
  [ -d venv ] || python3 -m venv --system-site-packages venv
  . venv/bin/activate
  # use KLayout from the Nix environment; pip wheels need Nix-provided libstdc++ and cairo
  grep -v "^klayout==" loom/tt/requirements.txt > tt-req.txt
  pip install -q -r tt-req.txt
  pip uninstall -q -y klayout 2>/dev/null || true
  GCCLIB=$(ls -d /nix/store/*-gcc-*-lib/lib 2>/dev/null | sort -V | tail -1)
  CAIROLIB=$(ls -d /nix/store/*-cairo-1.*[0-9]/lib 2>/dev/null | sort -V | tail -1)
  export LD_LIBRARY_PATH=$GCCLIB:$CAIROLIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
  python -c "import klayout.db, gdstk, cairosvg; print(\"python deps ok\")"
  cd loom
  python ./tt/tt_tool.py --pdk $PDK --create-user-config
  python ./tt/tt_tool.py --pdk $PDK --harden --no-docker
  python ./tt/tt_tool.py --pdk $PDK --print-warnings || true
  python ./tt/tt_tool.py --pdk $PDK --print-stats || true
'
echo HARDEN_DONE
RUN
chmod +x run.sh
nohup ./run.sh > harden.log 2>&1 &
echo "started on $(hostname), log: ~/$RDIR/harden.log"
EOF
  ;;
status)
  ssh "$HOST" "cd ~/$RDIR && ls -d loom/runs/wokwi/[0-9]* 2>/dev/null | tail -1; grep -E 'HARDEN_DONE|ERROR|harden failed' harden.log | tail -3; tail -2 harden.log"
  ;;
fetch)
  mkdir -p runs
  ssh "$HOST" "cd ~/$RDIR/loom && tar czf - runs/wokwi/final runs/wokwi/resolved.json runs/wokwi/flow.log runs/wokwi/warning.log runs/wokwi/error.log 2>/dev/null; true" | tar xzf - -C .
  ls runs/wokwi/final 2>/dev/null
  ;;
esac
