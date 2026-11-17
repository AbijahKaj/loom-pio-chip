#!/bin/sh
# Map to the IHP CMOS5L std-cell library and report area + timing estimate.
# usage: synth/area.sh [liberty]   (run from the repo root)
LIB=${1:-${IHP_LIB:-../pdk/sg13cmos5l_stdcell_typ_1p20V_25C.lib}}
mkdir -p build
yosys -p "
read_verilog -DSYNTH src/project.v src/loom_engine.v src/loom_fifo.v src/loom_spi.v
synth -top tt_um_abijahkaj_loom -flatten
dfflibmap -liberty $LIB
abc -liberty $LIB -D 16000
opt_clean
tee -o build/area.txt stat -liberty $LIB
" > build/area.log 2>&1 || { tail -20 build/area.log; exit 1; }
grep -E "Chip area|cells$" build/area.txt
