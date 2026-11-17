#!/bin/sh
# Post-synthesis gate-level netlist (IHP CMOS5L cells) for simulation: build/loom_gl.v
# Run from the repo root. Uses the same mapping as synth/area.sh.
LIB=${IHP_LIB:-../pdk/sg13cmos5l_stdcell_typ_1p20V_25C.lib}
mkdir -p build
yosys -q -l build/gl_netlist.log -p "
read_verilog src/project.v src/loom_engine.v src/loom_fifo.v src/loom_spi.v
synth -top tt_um_abijahkaj_loom -flatten
dfflibmap -liberty $LIB
abc -liberty $LIB -D 16000
hilomap -singleton -hicell sg13cmos5l_tiehi L_HI -locell sg13cmos5l_tielo L_LO
opt_clean
write_verilog -noattr -noexpr build/loom_gl.v
" && grep -c "sg13cmos5l_" build/loom_gl.v
