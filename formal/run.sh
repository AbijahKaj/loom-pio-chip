#!/bin/sh
# Formal checks with plain yosys (no SymbiYosys needed). Run from the repo root.
set -e
mkdir -p build
echo "== loom_fifo vs reference queue (k-induction, unbounded proof)"
yosys -q -l build/formal_fifo.log -p "
read_verilog -formal src/loom_fifo.v formal/fifo_props.v
prep -top fifo_props -flatten
memory
async2sync
dffunmap
sat -verify -tempinduct -prove-asserts -set-init-zero -seq 1 -set-at 1 rst_n 0 -maxsteps 12 -show-ports
" && echo "   PASS"
echo "== loom_engine assertions (bounded model check, 14 cycles from reset)"
yosys -q -l build/formal_engine.log -p "
read_verilog -formal -DFORMAL src/loom_engine.v src/loom_fifo.v
prep -top loom_engine -flatten
memory
async2sync
dffunmap
sat -verify -seq 14 -prove-asserts -set-init-zero -set-at 1 rst_n 0 -timeout 1800
" && echo "   PASS"
