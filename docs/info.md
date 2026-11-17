## How it works

Loom is a **programmable protocol engine**. Four small, cycle-exact state machines ("bit engines"), similar to
the RP2040's PIO, run user programs from a shared 32-instruction memory and drive 20 GPIOs. Every instruction
takes exactly one engine tick (plus an optional delay), so protocol timing is exact and predictable.

Each engine has **line helpers**, generic hardware for the hard parts of real-world protocols:

* bit-stuffing (USB/HDLC style or CAN style), NRZI and Manchester encode/decode, differential output;
* a programmable CRC/LFSR (any polynomial up to 32 bits, with a skip count for headers);
* edge timestamps, DPLL clock recovery, pin-pattern events and event traps (e.g. jump on USB end-of-packet).

With these, the same silicon speaks UART, SPI (controller modes 0–3, and target), I2C (with clock stretching),
low-speed USB (TX and RX), 10BASE-T transmit, Manchester receive, CAN (TX and RX), WS2812, PS/2 and pulse capture, all from firmware. The programs are
in `programs/`. The full description is in `docs/architecture.md` and `docs/isa.md`.

The host (the demo board's RP2350) controls Loom over SPI. It loads programs, configures the engines and
moves data through each engine's TX/RX FIFOs. An optional quad-write mode quadruples write bandwidth for streaming.

## How to test

1. Hold `rst_n` low, then release it. Read register 0x00 over SPI (mode 0, ≤ 7.5 MHz at a 60 MHz clock); it
   returns `0x4C4F4F4D` ("LOOM"). A command byte is `{rw, addr[6:0]}`, followed by little-endian 32-bit words.
2. Use the MicroPython driver `host/loomhost.py` together with the assembler `tools/loom/asm.py` (assemble on a PC, then
   copy the hex words), or assemble ahead of time. Example, UART TX at 115200 baud on GPIO13 (`uo[1]`):
   * write the four `uart_tx` instructions to `IMEM_DATA` (0x05);
   * configure engine 0: `CLKDIV` = 60 MHz / (4 × 115200), `out_base = sideset_base = 13`, `out_shift_right = 1`,
     side-set 1 bit optional;
   * write `GPIO` (0x07) = `1 << 13` (line idle high), then `CTRL` (0x01) = `0x11` (enable + restart engine 0);
   * push bytes to `TXF` (0x14) and watch them arrive on `uo[1]`.
3. Loop `uo[1]` back to `ui[3]` and load `uart_rx` on engine 1 to receive the bytes again.

## External hardware

* None for UART, SPI, PS/2, WS2812 or pulse-capture demos (3.3 V logic, Pmod-compatible headers).
* I2C: pull-up resistors (e.g. 4.7 kΩ) on GPIO0/GPIO1 (`uio[0]`, `uio[1]`), which run in open-drain mode.
* USB low speed: series resistors and a 1.5 kΩ pull-up on D- (device side); 3.3 V signalling.
* 10BASE-T: a magnetics/transformer Pmod on the differential TX pair; CAN: a CAN transceiver Pmod.
