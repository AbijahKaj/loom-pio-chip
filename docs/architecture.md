# Loom: a programmable protocol engine ASIC

Loom is an entry for the Jane Street × Tiny Tapeout protocol emulator competition. It targets
IHP 130 nm CMOS5L, 8x4 tiles (~1.2 mm² die), and a 60 MHz clock.

The chip has four small, cycle-exact **bit engines** in the spirit of the RP2040's PIO. Each engine also has
**line helpers**: generic hardware for the parts of real protocols that a plain shift-and-toggle machine handles
badly. Those are bit-stuffing, NRZI and Manchester coding, CRCs, clock recovery, pulse timing and
end-of-packet detection. With the helpers, the same silicon runs UART, SPI and I2C (controller and target), and
also low-speed USB, 10BASE-T transmit, CAN, WS2812, PS/2 and pulse capture, all from firmware.

See also [isa.md](isa.md) (instruction reference), [verification.md](verification.md) and
[info.md](info.md) (Tiny Tapeout datasheet page).

## 1. Design goals

1. **Flexibility first.** No protocol-specific hardware. Every helper is a generic, configurable primitive
   (a programmable LFSR, not "the USB CRC"; a run-length stuffer, not "USB stuffing"), so it can serve protocols
   that were never planned for.
2. **Cycle-exact timing.** One instruction per engine tick (plus a programmable delay), so protocol timing can be
   worked out on paper and is reproduced exactly by the Python model.
3. **Simple programs.** The "repeat-without-consume" rule (§4.1) keeps every coded serial loop at one instruction
   per line symbol. Helper events can redirect the program counter (traps), so packet boundaries need no polling.
4. **Verifiable.** A cycle-exact Python model is the golden reference. The RTL is checked against it state by state.

## 2. Top level

```
                 ┌──────────────────────────── tt_um_abijahkaj_loom ─────────────────────────────┐
  SCK   ui[0] ──►│                                                                               │
  CS_N  ui[1] ──►│  2-FF sync ─► host SPI target ─► register bus ─► imem write, engine config,   │
  MOSI  ui[2] ──►│               (quad-write mode)                  FIFOs, IRQ, GPIO, HOSTCFG    │
  MISO  uo[0] ◄──│                                                                               │
                 │        ┌──── 4 × IHP SRAM 64×16 (one per engine, addressed by next-PC) ────┐   │
                 │   ┌────┴────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐                     │   │
                 │   │ engine0 │  │ engine1 │  │ engine2 │  │ engine3 │ ◄── 8 shared IRQ flags   │
                 │   │ +coder  │  │ +coder  │  │ +coder  │  │ +coder  │ ◄── 16-bit timestamp     │
                 │   │ +CRC    │  │ +CRC    │  │ +CRC    │  │ +CRC    │                          │
                 │   │ +events │  │ +events │  │ +events │  │ +events │                          │
                 │   │ TX/RX   │  │ TX/RX   │  │ TX/RX   │  │ TX/RX   │                          │
                 │   │ FIFOs   │  │ FIFOs   │  │ FIFOs   │  │ FIFOs   │                          │
                 │   └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘                          │
                 │        └── pin-write merge (higher engine wins) + open-drain ──┐              │
                 └────────────────────────────────────────────────────────────────┼──────────────┘
                                         20 GPIO: uio[7:0], ui[7:3], uo[7:1]
```

### 2.1 Pins

| GPIO | Pad | Direction | Notes |
|---|---|---|---|
| 0–7 | `uio[0..7]` | bidirectional | Per-pin output enable (PINDIRS/OE) or open-drain mode (`OD` register: 0 drives low, 1 releases). Used for I2C and USB D+/D-. |
| 8–12 | `ui[3..7]` | input | 2-FF synchronised. In quad-write mode, GPIO8–10 double as host data lines IO1–IO3. |
| 13–19 | `uo[1..7]` | output | Always driven. Reading them returns the output register. |
| — | `ui[0..2]`, `uo[0]` | host SPI | SCK, CS_N, MOSI, MISO |

Every engine can read and write any GPIO, and pin bases wrap modulo 32 (pins 20–31 read as 0). When several engines
write the same pin in a cycle, the highest-numbered engine wins, as on PIO.

### 2.2 Host interface

The host is an SPI target (mode 0, oversampled; SCK ≤ clk/8, so 7.5 MHz at 60 MHz). The demo board's RP2350 is the
intended host; see `host/loomhost.py`.

A transaction is: `CS_N` low, a command byte `{rw, addr[6:0]}`, then 4 bytes per 32-bit word (little-endian, MSB-first
within each byte). The address auto-increments after each word, except on the FIFO/IMEM "port" registers, so whole
streams go in one transaction.

- **Reads** peek a word when it starts and only *commit* a FIFO pop once the host clocks that word, so a trailing
  read-ahead never loses data.
- **Quad-write mode** (`HOSTCFG[0]`) moves write data on `{GPIO10, GPIO9, GPIO8, MOSI}`, 4 bits per clock (~30 Mb/s).
  This is what makes full-rate 10BASE-T transmit possible.

| Addr | Register | Access | Description |
|---|---|---|---|
| 0x00 | ID | R | `0x4C4F4F4D` ("LOOM") |
| 0x01 | CTRL | RW | [3:0] engine enable; write-1 pulses: [7:4] restart, [11:8] FIFO clear, [15:12] clock-divider sync |
| 0x02 | FLEVEL | R | engine n: [8n+2:8n] TX FIFO level, [8n+6:8n+4] RX FIFO level |
| 0x03 | IMEM_ADDR | RW | instruction-memory write pointer |
| 0x05 | IMEM_DATA | W | `imem[ptr++] = data[15:0]` |
| 0x06 | IRQ | RW | read the flags; write: [7:0] clear, [15:8] set |
| 0x07 | GPIO | RW | read the 20-bit input vector; write the GPIO output register |
| 0x08 | OD | RW | open-drain enable for GPIO0–7 |
| 0x09 | OE | RW | output enable for GPIO0–7 (non open-drain) |
| 0x0A | HOSTCFG | RW | [0] quad-write mode |
| 0x10·(n+1)+0 | CLKDIV | W | [31:16] integer, [15:8] fraction |
| +1 | EXECCFG | W | [5:0] wrap_bottom, [11:6] wrap_top, [17:12] start, [20:18] sideset_count, [21] opt, [22] side→pindirs, [27:23] jmp_pin |
| +2 | SHIFTCFG | W | [0] in_shift_right, [1] out_shift_right, [2] autopush, [3] autopull, [8:4] push_thresh, [13:9] pull_thresh (0 = 32) |
| +3 | PINCFG | W | [4:0] out_base, [10:5] out_count, [15:11] set_base, [18:16] set_count, [23:19] side_base, [28:24] in_base |
| +4 | TXF | W | push to the TX FIFO |
| +5 | RXF | R | pop from the RX FIFO (0 if empty) |
| +6 | CODECFG | W | [1:0] mode (0 none, 1 NRZI, 2 Manchester), [2] NRZI toggle-on value, [3] Manchester polarity, [4] stuff enable, [5] stuff mode, [9:6] stuff run length, [10] differential, [11] idle level, [16:12] ts_pin, [18:17] ts_edge (1 rise, 2 fall, 3 both), [19] DPLL |
| +7 | CRCCFG | W | [5:0] width (0 = 32), [6] init all-ones, [12:7] skip count |
| +8 | CRCPOLY | W | polynomial (MSB-first Galois form, implicit x^width) |
| +9 | CRCVAL | W | set the CRC register (and re-load the skip count) |
| +B/+C | PATMASK / PATVAL | W | 20-bit pin pattern for event 0 |
| +D | TRAPCFG | W | [0] enable/arm, [3:1] event, [4] polarity, [10:5] address, [31:16] timeout (event 3) |
| +E | DBG | R | {osr_cnt, flags, isr_cnt, delay, pc} |
| +F | X | R | engine X register |

## 3. Bit engine

Each engine holds:

- **Execution state:** `PC`, scratch registers `X` and `Y` (32 bits), shift registers `OSR` and `ISR` (32 bits, with counters)
- **Clocking:** a 16.8 fractional clock divider and a 4-bit delay counter
- **FIFOs:** a 2-deep × 32-bit TX FIFO and RX FIFO
- **Helpers:** coder, CRC, timestamp and trap state (§4)

Execution, per tick:

1. If the trap is armed and its event is true, jump to the trap address.
2. Otherwise, if a delay is pending, count it down.
3. Otherwise, execute `imem[pc]`.

A stalled instruction (blocking FIFO, `wait`, `irq wait`) has no effect except side-set. The formal checks
prove this property (see [verification.md](verification.md)).

**Area-motivated structure.** All pin writes go through one rotate unit, and `IN`/`OUT` share one 64→32 funnel
shifter and one left shifter. Splitting these out per instruction cost about 80k µm² in the first synthesis.

## 4. Line helpers

All helpers sit on the **coded bit path** and only affect `in.c` / `out.c`. Plain `in` / `out` behave like PIO.

```
 TX:  OSR ─► CRC tap (after skip) ─► bit-stuffer ─► NRZI | Manchester ─► pin (+ complement on pin+1)
 RX:  pin ─► NRZI | Manchester decode ─► de-stuffer (error flag) ─► CRC tap ─► ISR ─► autopush
```

### 4.1 Repeat-without-consume

A coded op moves one line symbol per execution. A symbol that carries no data (a stuff bit, or the first half of a
Manchester bit) makes the instruction **execute again without consuming or producing data**. Two consequences:

- One instruction per symbol keeps timing trivial. `out.c pins, x` streams an entire USB or Ethernet packet with
  zero gaps: X counts the data bits, and autopull refills the OSR in the same cycle.
- The op only completes when no stuff bit is owed. So a USB packet ending in six ones gets its final stuff bit,
  and the receiver discards it, without any special code.

### 4.2 CRC / LFSR

Each engine has a 32-bit Galois LFSR with programmable polynomial, width (1–32), init value (zero or all-ones;
anything else via `mov crc, x`) and **skip count**. The skip count excludes the first N data bits after a reset,
e.g. a USB PID.

Every coded data bit is fed through it. `in crc` / `mov … crc` read it (combine with `~` and `::` for inversion
and reflection), and event 2 compares it with Y for residue checks. This one block covers USB CRC5/CRC16, CAN
CRC15, Ethernet CRC32, HDLC/X.25 CRC16, SMBus PEC and others.

### 4.3 Timestamps, clock recovery, traps

* A global 16-bit cycle counter. Each engine records the interval between edges on its `ts_pin` (rising, falling
  or both); `in tstamp, 16` reads it. Used for pulse capture, IR decoding, 1-Wire timing and auto-baud. Event 3
  (timeout) fires when no edge has arrived for `TIMEOUT` cycles.
* **DPLL:** each edge re-phases the clock divider to mid-bit, and a tick that would coincide with the edge is
  suppressed, so the first sample after any edge lands mid-symbol. The USB, CAN and Manchester receivers use it
  (tested with 1.5%, 0.8% and 1% clock errors).
* **Traps:** any event can redirect the PC, e.g. on USB SE0 (end of packet) or SPI CS release, with no polling loop.

## 5. Software

| Path | Contents |
|---|---|
| `tools/loom/asm.py` | Assembler (PIO-flavoured syntax, `.side_set`, `.wrap`, `.define`, labels, expressions) |
| `tools/loom/sim.py` | Cycle-exact golden model of the whole chip below the SPI layer |
| `tools/loom/regs.py` | Register map and `EngineConfig` encoder (shared by tests and docs) |
| `programs/*.loom` | 16 programs: `uart_tx`, `uart_rx`, `spi_cpha0`, `spi_cpha1` (modes 0–3 via `CPOL`), `spi_target`, `i2c_ctrl`, `usb_ls_tx`, `usb_ls_rx`, `eth10_tx`, `eth10_nlp`, `manchester_rx`, `can_tx`, `can_rx`, `ws2812`, `ps2_rx`, `pulse_capture` |
| `host/loomhost.py` | MicroPython host driver for the RP2350 |

## 6. Implementation numbers

The design is synthesised with LibreLane's `AREA 0` strategy. The default `DELAY 4` strategy inserted about 34k
buffers on this design.

| Item | Value |
|---|---|
| Std-cell area (Yosys/ABC, typ corner, pre-PnR) | ~459k µm² (~38% of the 8x4 core) |
| Flip-flops | ~3.8k |
| Instruction memory | **SRAM variant (this branch):** four IHP `RM_IHPSG13_1P_64x16_c2` macros, one 64-instruction bank per engine, addressed with the engine's *next* PC so the registered SRAM output always holds `imem[pc]` (cycle-identical to the flip-flop version). **Flip-flop variant (`main`):** 32 × 16 flops with 4 read ports. |
| Clock | Signed off at 25 ns (40 MHz) at all corners; routed critical path ≈16.4 ns at typical (60 MHz capable at nominal conditions), ≈26 ns at slow 1.08 V/125 °C |
| Critical path | PC → 32:1 instruction-memory read → decode (high fanout) → execute, in one cycle. A registered instruction fetch is the next step for worst-case 60 MHz. |

The first full PnR attempts (64-entry imem, density 60%) placed and closed timing at 20 ns with +6.8 ns slack after
CTS. Global routing then overflowed on Metal3, which led to the 32-entry memory and the density/derating changes.
Final numbers are in the hardening summary of the submission.

## 7. Known limitations

* The host link (≤7.5 MHz single, ~30 Mb/s quad-write) bounds sustained streaming. 10BASE-T transmit needs paced
  quad writes. 10BASE-T *receive* at the full 10 Mb/s (3 clocks per half-bit, behind a 2-FF synchroniser) is not
  claimed; the same Manchester receive path is demonstrated at 1 Mb/s (`manchester_rx`).
* FIFOs are 2 deep. Fast receive protocols should use autopush at 32 bits so the host has time to drain.
* SPI target mode supports SCK up to about f_clk/24 (synchroniser plus instruction latency).
* I2C *target* (`i2c_target`, 47 instructions) needs a 64-instruction bank, so it is available on the SRAM
  variant only. It uses the *qualified-edge* event (event 7: an edge on `ts_pin` while the pin pattern matches),
  which detects START/STOP as "SDA changed while SCL is high".
