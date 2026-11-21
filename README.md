# Loom: a programmable protocol engine for Tiny Tapeout

Loom is a small ASIC (IHP 130 nm CMOS5L, 8x4 Tiny Tapeout tiles, 60 MHz) that speaks hardware protocols defined in
**firmware**. It has four cycle-exact bit engines, in the spirit of the RP2040's PIO. Each engine has generic **line
helpers**: bit-stuffing, NRZI and Manchester coding, a programmable CRC, edge timestamps, DPLL clock recovery, and
pattern events that can trap the program counter. Together they let the same silicon run:

| Protocol | Program | Verified on RTL (real SPI host) |
|---|---|---|
| UART TX/RX (framing-error IRQ) | `uart_tx`, `uart_rx` | ✔ |
| SPI controller, modes 0/1/2/3 | `spi_cpha0`, `spi_cpha1` | ✔ |
| SPI target (CS-abort via trap) | `spi_target` | ✔ |
| I2C controller (repeated start, NACK, clock stretching) | `i2c_ctrl` | ✔ |
| I2C target (address match, write/read, repeated START, STOP via qualified-edge trap) | `i2c_target` | ✔ |
| Low-speed USB TX (NRZI + stuffing + EOP) | `usb_ls_tx` | ✔ |
| Low-speed USB RX (DPLL, de-stuffing, CRC16 residue check, ±1.5 % clock) | `usb_ls_rx` | ✔ |
| 10BASE-T TX at full rate (Manchester, quad-SPI host feed) | `eth10_tx`, `eth10_nlp` | ✔ (TX) |
| CAN 2.0 TX and RX (CAN-style stuffing, EOF via stuff-error event, ±0.8 % clock) | `can_tx`, `can_rx` | ✔ |
| Manchester RX (10BASE-T framing at reduced rate, DPLL, ±1 % clock) | `manchester_rx` | ✔ |
| WS2812, PS/2 RX, pulse/interval capture | `ws2812`, `ps2_rx`, `pulse_capture` | ✔ |
| Four protocols concurrently | — | ✔ |

## Documentation

* [docs/architecture.md](docs/architecture.md): design, pins, register map, line helpers, area
* [docs/isa.md](docs/isa.md): instruction set reference
* [docs/verification.md](docs/verification.md): lockstep golden-model testing, protocol models, formal, gate level, bug log
* [AI assistance](docs/verification.md#6-tools-and-ai-assistance): how AI tools were used and how their output was checked
* [docs/hardening.md](docs/hardening.md): flow settings and PnR history
* [docs/info.md](docs/info.md): Tiny Tapeout datasheet page

## Layout

```
src/            RTL: project.v (top), loom_engine.v, loom_spi.v, loom_fifo.v, config.json
programs/       Loom assembly programs (*.loom)
tools/loom/     assembler, cycle-exact ISS (golden model), register map, export tool
host/           MicroPython host driver for the RP2350 (loomhost.py)
test/           cocotb tests, device models, protocol scenarios, ISS runner
formal/         Yosys formal checks (FIFO induction proof, engine BMC)
synth/          area estimate, gate-level netlist, hardening scripts
```

## Quick start

```sh
python3 -m venv .venv && .venv/bin/pip install -r test/requirements.txt   # Python <= 3.13 for cocotb 2.0.1
python3 test/run_iss.py                       # all protocol scenarios on the ISS (seconds)
cd test && make -B NODUMP=1 COCOTB_TEST_MODULES=test,test_protocols,test_diff,test_host   # RTL
formal/run.sh                                 # formal checks
synth/area.sh                                 # area estimate with the IHP liberty
```

Assemble a program: `tools/loomasm programs/uart_tx.loom` (listing) or `tools/loomasm programs/uart_tx.loom --hex`.
