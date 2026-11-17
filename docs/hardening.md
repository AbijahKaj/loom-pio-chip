# Hardening (GDS) and sign-off

## Flow settings (`src/config.json`)

| Setting | Value | Why |
|---|---|---|
| `SYNTH_STRATEGY` | `AREA 0` | LibreLane's default `DELAY 4` ABC script inserted ~34k buffers on this design (947k µm² instance area, detailed placement failed). `AREA 0` maps it to ~460k µm². Timing is then fixed in PnR by the resizer. |
| `PL_TARGET_DENSITY_PCT` | 50 | Spread the cells: only Metal2–4 are available for signal routing on the TT IHP tiles. |
| `GRT_ADJUSTMENT` | 0.2 | Default 0.3. Global routing itself reported that the derating was too pessimistic. |
| `CLOCK_PERIOD` | 16 ns | 62.5 MHz constraint, for operation at 60 MHz with margin. |

## History of runs

| Run | Design | Result |
|---|---|---|
| 1 | 64×16 imem, `DELAY 4`, density 60 | Synthesis 947k µm² (34k buffers). Detailed placement failed after 3.1k hold buffers. |
| 2 | 64×16 imem, `AREA 0`, density 60 | Synthesis 533k µm²; post-CTS utilisation 54%, **setup WNS +6.8 ns at 20 ns**. Global routing overflow 8.9k (Metal3 at 89%); detailed routing started with ~36k violations, so the run was stopped. |
| 3 | 32×16 imem, density 50, GRT 0.2, 16 ns | Pending (to be run on the Linux build machine). |

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
