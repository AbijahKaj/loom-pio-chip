# Hardening (GDS) and sign-off

## Flow settings (`src/config.json`)

| Setting | Value | Why |
|---|---|---|
| `SYNTH_STRATEGY` | `AREA 0` | LibreLane's default `DELAY 4` ABC script inserted ~34k buffers on this design (947k µm² instance area, detailed placement failed). `AREA 0` maps it to ~460k µm². Timing is then fixed in PnR by the resizer. |
| `PL_TARGET_DENSITY_PCT` | 50 | Spread the cells: only Metal2–4 are available for signal routing on the TT IHP tiles. |
| `GRT_ADJUSTMENT` | 0.2 | Default 0.3. Global routing itself reported that the derating was too pessimistic. |
| `CLOCK_PERIOD` | 33 ns | Sign-off at **all** corners (TT sets `TIMING_VIOLATION_CORNERS = *`): 30 MHz with margin at slow 1.08 V/125 °C; ~52 MHz at typical. |
| `MAX_TRANSITION_CONSTRAINT`, `RUN_POST_GRT_RESIZER_TIMING`, `PNR_CORNERS` | 1.0 ns, on, typ+slow | Without these, PnR optimised only the typical corner and left hundreds of slow-corner slew violations (runs 3–4). |
| `PL_/GRT_RESIZER_SETUP_SLACK_MARGIN` | 6 / 4 ns | Keeps the optimiser working as if the clock were faster; a relaxed constraint alone let slow paths grow (run 4). |

## History of runs

| Run | Design | Result |
|---|---|---|
| 1 | 64×16 imem, `DELAY 4`, density 60 | Synthesis 947k µm² (34k buffers). Detailed placement failed after 3.1k hold buffers. |
| 2 | 64×16 imem, `AREA 0`, density 60 | Synthesis 533k µm²; post-CTS utilisation 54%, **setup WNS +6.8 ns at 20 ns**. Global routing overflow 8.9k (Metal3 at 89%); detailed routing started with ~36k violations, so the run was stopped. |
| 3 | 32×16 imem, density 50, GRT 0.2, 16 ns (Linux, Nix, no Docker) | **Routed clean**: global-routing overflow 0; detailed routing 0 violations after 2 antenna-diode rounds (137 + 18 diodes); **Magic DRC 0, LVS pass, antenna pass**; hold met at all corners. Setup: fast +5.2 ns, **typical −0.4 ns (≈16.4 ns path → 60 MHz passes at 16.67 ns)**, slow −10.1 ns (≈26 ns path). The flow failed only on setup at 16 ns. |
| 4 | as run 3, `CLOCK_PERIOD` 25 ns | Routed clean, but setup: typical +5.0 ns, **slow −7.1 ns** (70 paths). With the relaxed constraint the resizer did less, and PnR optimised only the typical corner, with no slew limit (265 slow-corner slew violations). Stopped. |
| 5 | as run 4 + `MAX_TRANSITION_CONSTRAINT` 1.0, `RUN_POST_GRT_RESIZER_TIMING`, `PNR_CORNERS` typ+slow | **DRC, LVS, antenna pass**; hold met everywhere; setup typical **+5.7 ns at 25 ns**, fast +12.7 ns, slow **−6.2 ns** (23 paths). Post-CTS slow slack was +5.7 ns: routed wires on 3 layers cost about 12 ns. Critical path: about 7.5 ns of weak `buf_1` fan-out on PC bits into the 32:1 imem mux, then decode, then execute. |
| 6 | as run 5, `CLOCK_PERIOD` 33 ns, resizer setup margins 6 / 4 ns | ✅ **Signed off.** GDS written; **DRC, LVS and antenna pass**; setup/hold met at **all** corners: fast +20.7 ns, typical **+13.8 ns** (≈19.2 ns path → ~52 MHz at nominal conditions), slow **+1.9 ns** (≈31.1 ns path → 32 MHz guaranteed). Utilisation 48%. |

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

## Gate-level simulation

**On the routed netlist (run 6, official `GATES=yes` flow):** the smoke test and all 24 protocol/host tests pass
(25/25), including USB with clock skew, full-rate 10BASE-T over quad-SPI, CAN/Manchester receive and the
four-engine concurrent test.

### Post-synthesis (earlier)

`make -B GATES=synth` simulates the post-synthesis IHP-cell netlist (from `synth/gl_netlist.sh`) with the vendor
cell models. The smoke test, all 22 protocol scenarios and the host-driver test pass on it (24/24, ~6 min).
