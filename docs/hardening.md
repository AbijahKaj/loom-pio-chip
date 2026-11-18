# Hardening (GDS) and sign-off

## Flow settings (`src/config.json`)

| Setting | Value | Why |
|---|---|---|
| `SYNTH_STRATEGY` | `AREA 0` | LibreLane's default `DELAY 4` ABC script inserted ~34k buffers on this design (947k µm² instance area, detailed placement failed). `AREA 0` maps it to ~460k µm². Timing is then fixed in PnR by the resizer. |
| `PL_TARGET_DENSITY_PCT` | 50 | Spread the cells: only Metal2–4 are available for signal routing on the TT IHP tiles. |
| `GRT_ADJUSTMENT` | 0.2 | Default 0.3. Global routing itself reported that the derating was too pessimistic. |
| `CLOCK_PERIOD` | 25 ns | Sign-off at **all** corners (TT sets `TIMING_VIOLATION_CORNERS = *`) for 40 MHz worst case. See run 3 for the typical-corner 60 MHz figure. |

## History of runs

| Run | Design | Result |
|---|---|---|
| 1 | 64×16 imem, `DELAY 4`, density 60 | Synthesis 947k µm² (34k buffers). Detailed placement failed after 3.1k hold buffers. |
| 2 | 64×16 imem, `AREA 0`, density 60 | Synthesis 533k µm²; post-CTS utilisation 54%, **setup WNS +6.8 ns at 20 ns**. Global routing overflow 8.9k (Metal3 at 89%); detailed routing started with ~36k violations, so the run was stopped. |
| 3 | 32×16 imem, density 50, GRT 0.2, 16 ns (Linux, Nix, no Docker) | **Routed clean**: global-routing overflow 0; detailed routing 0 violations after 2 antenna-diode rounds (137 + 18 diodes); **Magic DRC 0, LVS pass, antenna pass**; hold met at all corners. Setup: fast +5.2 ns, **typical −0.4 ns (≈16.4 ns path → 60 MHz passes at 16.67 ns)**, slow −10.1 ns (≈26 ns path). The flow failed only on setup at 16 ns. |
| 4 | as run 3, `CLOCK_PERIOD` 25 ns | Routed clean, but setup: typical +5.0 ns, **slow −7.1 ns** (70 paths). With the relaxed constraint the resizer did less, and PnR optimised only the typical corner, with no slew limit (265 slow-corner slew violations). Stopped. |
| 5 | as run 4 + `MAX_TRANSITION_CONSTRAINT` 1.0, `RUN_POST_GRT_RESIZER_TIMING`, `PNR_CORNERS` typ+slow | **DRC, LVS, antenna pass**; hold met everywhere; setup typical **+5.7 ns at 25 ns**, fast +12.7 ns, slow **−6.2 ns** (23 paths). Post-CTS slow slack was +5.7 ns: routed wires on 3 layers cost about 12 ns. Critical path: about 7.5 ns of weak `buf_1` fan-out on PC bits into the 32:1 imem mux, then decode, then execute. |
| 6 | as run 5, `CLOCK_PERIOD` 33 ns, resizer setup margins 6 / 4 ns | Running |

## Running it

```sh
synth/area.sh        # quick Yosys area estimate with the IHP liberty (no Docker)
synth/gl_netlist.sh  # post-synthesis netlist with IHP cells, for gate-level simulation
synth/harden.sh      # full LibreLane flow + TT precheck tooling (Docker or Nix)
```

After hardening, run the official gate-level test on the PnR netlist:

```sh
cp runs/wokwi/final/nl/tt_um_abijahkaj_loom.nl.v test/gate_level_netlist.v
cd test && make -B GATES=yes
```

## Gate-level simulation already done

`make -B GATES=synth` simulates the post-synthesis IHP-cell netlist (from `synth/gl_netlist.sh`) with the vendor
cell models. The smoke test, all 22 protocol scenarios and the host-driver test pass on it (24/24, ~6 min).


## SRAM variant (branch `sram-imem`)

Four `RM_IHPSG13_1P_64x16_c2` macros (236.8 × 64.36 µm, one per engine) sit flipped (`FS`) along the bottom edge
with their Metal2 pins facing the logic. The macros' supply pins are **Metal4 columns, the same layer as the
tile's power stripes**, so pdngen cannot reach them through vias. The solution is a small hook in
`src/pdn_cfg.tcl`, with no custom flow steps:

1. **Stripe pitch 44.96 µm** (4 × the macro's 11.24 µm column pitch) and 5.62 µm VPWR→VGND spacing. Each macro is
   placed so that every stripe crossing it lands on a matching VDD/VSS column, at least 0.89 µm from the opposite
   net. An exhaustive offset search over the macro LEF chose this placement.
2. After pdngen, **each stripe that stops at an SRAM is extended across it**, overlapping the original stripe by 1 µm.
3. **VPWR/VGND pins are rebuilt** from the final stripe metal, so every pin spans the full tile height, as TT's pin
   check requires.

Result of the quick PDN run: 44 stripes extended, `check_power_grid` 0 errors on both nets, 38 full-height pins per net.

| Run | Result |
|---|---|
| 7 | Timing met at all corners at 33 ns with large margins: **slow +7.2 ns (~39 MHz worst case), typical +16.9 ns (~62 MHz)**, versus 32 / 52 MHz for flip-flops. LVS pass. Magic reported 34,721 DRC errors, **all inside the SRAM footprints** (0 elsewhere): Magic does not model the SRAM rule exceptions. 1 antenna net. The **TT precheck on this GDS: IHP KLayout DRC ✅, layer check ✅**; pin check ❌, because the power pins over the SRAMs did not reach the bottom edge (fixed by step 3). |
| 8 | KLayout DRC sign-off (IHP deck) instead of Magic, more antenna repair, pin rebuild | Running |
