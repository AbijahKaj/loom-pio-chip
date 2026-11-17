# Loom — a programmable protocol engine ASIC

**Status:** draft v0 spec (2026-11-17). Numbers marked *(est)* need to be confirmed by synthesis.

Loom is a protocol emulator for the Jane Street × Tiny Tapeout competition (IHP 130 nm CMOS5L, 8x4 tiles,
target clock 60 MHz). It is a set of small, cycle-exact **bit engines**, inspired by the RP2040's PIO. Each engine
has a set of **line helpers** that handle the parts of real-world protocols a PIO-style machine is bad at:
bit-stuffing, NRZI, Manchester coding, CRCs, clock recovery and pulse-width timing. Those helpers are
what let the same silicon run UART, SPI and I2C, and also reach low-speed USB, 10BASE-T, CAN and HDLC, all from firmware.

## 1. Design goals

1. **Flexibility first.** Nothing protocol-specific in hardware. Every helper is a generic, configurable
   primitive (an LFSR, not "the USB CRC"), so it can serve protocols we never planned for.
2. **Cycle-exact timing.** Every instruction takes exactly 1 cycle (plus a programmable delay) at the engine's
   divided clock, so timing can be checked with pencil and paper.
3. **Fits comfortably.** Target ≤ 60 % placement utilisation of 8x4 tiles, leaving room for CTS and routing.
4. **Easy to verify.** A Python instruction-set simulator (ISS) is the golden model. The RTL is
   differential-tested against it with random programs.

## 2. Top level

```
             ┌──────────────────────── tt_um_abijahkaj_loom ─────────────────────────┐
 SCK  ui[0] ─┤                                                                      │
 CS_N ui[1] ─┤  Host SPI    ──►  register file / imem write port / FIFO access      │
 MOSI ui[2] ─┤  target                                                              │
 MISO uo[0] ◄┤                                                                      │
             │        ┌────────── shared instruction memory 64 × 16 ──────────┐     │
             │        │      (4 read ports, 1 write port from host)          │     │
             │   ┌────┴───┐ ┌────────┐ ┌────────┐ ┌────────┐                 │     │
             │   │ Eng 0  │ │ Eng 1  │ │ Eng 2  │ │ Eng 3  │  ◄─ IRQ flags ──┘     │
             │   │+helpers│ │+helpers│ │+helpers│ │+helpers│     (8 shared)        │
             │   └───┬────┘ └───┬────┘ └───┬────┘ └───┬────┘                       │
             │       └────── GPIO crossbar / OR-merge / open-drain ──────┐         │
             └───────────────────────────────────────────────────────────┼─────────┘
                                           20 GPIO: uio[7:0], ui[7:3], uo[7:1]
```

### 2.1 Pin map

| GPIO # | Physical | Direction | Notes |
|---|---|---|---|
| 0–7 | `uio[0..7]` | bidirectional | per-pin OE, open-drain mode (I2C, 1-Wire, …) |
| 8–12 | `ui[3..7]` | input only | 2-FF synchronised |
| 13–19 | `uo[1..7]` | output only | |
| — | `ui[0..2]`, `uo[0]` | host SPI | reserved |

All inputs pass through a 2-flop synchroniser (2 cycles of latency, documented in the timing model).
When several engines drive the same pin, their outputs are OR-merged, and OE/OD is the OR of the OEs.

### 2.2 Host interface

SPI target, mode 0, oversampled by the system clock (SCK ≤ clk/6 ≈ 10 MHz). The demo board's RP2350 acts as the host.

Transaction: `CS_N↓`, command byte `{rw, addr[6:0]}`, then N × 8-bit data bytes. Registers are 32 bits,
little-endian, and the address auto-increments, except on FIFO addresses so streaming works.

| Addr | Register | Notes |
|---|---|---|
| 0x00 | `ID` | `"LOOM"` + version |
| 0x01 | `CTRL` | engine enable mask, per-engine restart, clock-divider resync |
| 0x02 | `STATUS` | FIFO full/empty flags, IRQ flags |
| 0x03 | `IMEM_ADDR` | write pointer for imem loading |
| 0x04 | `IMEM_DATA` | write 16 bits, auto-increment pointer |
| 0x08+4n.. | engine *n* `CLKDIV`, `PINCFG`, `EXECCFG`, `SHIFTCFG` | like PIO SMx_* registers |
| 0x18+4n.. | engine *n* `TXF`, `RXF`, `CRCCFG`, `CODECFG` | FIFOs + helper config |
| 0x30–0x33 | engine *n* `CRCPOLY` | 32-bit polynomial |

(The exact map gets frozen in week 2.)

## 3. Bit engine

Per-engine state: `PC[5:0]`, `X`, `Y` (32 b scratch), `OSR`, `ISR` (32 b shift registers, with shift counters),
a 16.8 fractional clock divider, a delay counter, and a 4 × 32-bit TX FIFO and RX FIFO.

`EXECCFG` sets `wrap_top`/`wrap_bottom` (free loops), `jmp_pin`, and `sideset_count` (0–4) with optional enable.
`SHIFTCFG` sets shift direction, autopush/autopull, and the thresholds.

### 3.1 ISA (16-bit instructions)

```
 15  13 12     9 8                     0
┌─────┬────────┬────────────────────────┐
│ op  │ dly/ss │        operands        │   dly/ss: 4 bits split between side-set and delay
└─────┴────────┴────────────────────────┘
```

| op | Mnemonic | Operands | Semantics |
|---|---|---|---|
| 0 | `JMP cond, addr` | cond[8:6], addr[5:0] | cond ∈ always, !X, X--, !Y, Y--, X≠Y, PIN, !OSRE |
| 1 | `WAIT pol, src, idx` | pol[8], src[7:6], idx[5:0] | src ∈ GPIO, PIN (relative), IRQ, EVENT (helper events) |
| 2 | `IN src, n` / `IN.c` | src[8:6], coded[5], n[4:0] | src ∈ PINS, X, Y, NULL, ISR, OSR, TSTAMP, CRC |
| 3 | `OUT dst, n` / `OUT.c` | dst[8:6], coded[5], n[4:0] | dst ∈ PINS, X, Y, NULL, PINDIRS, PC, ISR, EXEC |
| 4 | `PUSH`/`PULL` | dir[8], ifful/ifempty[7], block[6] | |
| 5 | `MOV dst, op src` | dst[8:6], op[5:4], src[2:0] | op ∈ none, invert, bit-reverse |
| 6 | `IRQ` | clr[8], wait[7], idx[2:0] | 8 shared flags for inter-engine sync |
| 7 | `SET dst, imm` | dst[8:6], imm[5:0] | dst ∈ PINS, X, Y, PINDIRS, **HELPER** |

`SET HELPER, imm` is the in-program control port for the helpers (reset CRC, arm or disarm the stuffer, clear the timestamp,
resync the clock). Programs can therefore switch helper modes mid-packet, for example at a USB SYNC/EOP.

## 4. Line helpers (the differentiators)

All helpers sit on the **coded bit path**. They only affect `IN.c` / `OUT.c` (the coded bit set), so plain
`IN`/`OUT` behaves exactly like PIO and the helpers can be ignored.

```
 TX:  OSR ──► CRC tap ──► bit-stuffer ──► NRZI / Manchester encoder ──► pin
 RX:  pin ──► NRZI / Manchester decoder ──► de-stuffer ──► CRC tap ──► ISR
```

### 4.1 The "repeat-without-consume" rule

This is the core idea that keeps the engine simple. A coded op moves one *line symbol*. When the helper has to
emit or swallow a symbol that carries no data, the instruction **re-executes without consuming or producing a data bit**:

* **Bit-stuffing TX:** after N identical bits, `OUT.c` emits the stuff bit and leaves the OSR alone. The program's
  bit loop keeps its exact timing and needs no special case.
* **Bit-stuffing RX:** `IN.c` on a stuffed bit discards it and leaves the ISR alone.
* **Manchester:** each data bit is two half-bit symbols. The first `OUT.c` emits the first half and repeats.
  The second emits the other half and consumes the bit.

Because one loop iteration always equals one line symbol, the timing analysis stays trivial.

### 4.2 Configurable CRC/LFSR

Each engine has a 32-bit Galois LFSR with a programmable polynomial, width (1–32), init value, reflect-in/out,
and xor-out. It sees every *data* bit of coded ops: pre-stuff on TX, post-destuff on RX. `IN CRC, n` / `MOV x, CRC`
reads it, and `WAIT EVENT CRC_OK` checks a residue match. This one block covers USB CRC5/CRC16, Ethernet CRC32,
CAN CRC15, HDLC CRC16, SMBus PEC (CRC8), and others.

### 4.3 Edge timestamps and clock recovery

* A global 16-bit cycle counter. Each engine captures it on a configurable edge of a configurable pin.
  `IN TSTAMP, n` returns the time since the previous edge. That enables auto-baud, pulse-width
  protocols (WS2812 RX, IR remotes, 1-Wire timing, PWM capture) and logic-analyser-style capture.
* **DPLL-lite:** when enabled, an edge on the recovery pin re-phases the engine's clock divider to
  mid-bit. This keeps the sampling point centred for USB (±0.25 %) and Manchester RX without oversampling loops.

### 4.4 Events

`WAIT EVENT` sources: pin pattern match (mask/value over the 20 GPIOs), stuff error (USB EOP / HDLC abort
detection), CRC residue OK, timestamp > threshold (timeouts, I2C clock stretch, idle detect).

## 5. Software

* `tools/loomasm` — a Python assembler (PIO-like syntax, with `.side_set`, `.wrap`, `.define`, and helper
  directives such as `.crc poly=0x8005 width=16 reflect`).
* `tools/loomsim` — the Python ISS and golden model. The cocotb tests use it as the reference model.
* `host/` — a MicroPython driver for the demo board's RP2350: load programs, configure engines, stream FIFOs.
* `programs/` — `uart_tx`, `uart_rx`, `spi_ctrl` (modes 0–3), `spi_target`, `i2c_ctrl` (100/400 kHz, with clock stretch),
  `i2c_target`, then stretch goals `usb_ls`, `eth10_tx`/`rx`, `can`, `ws2812`, `onewire`, `ps2`, `jtag`.

## 6. Verification strategy

1. **Unit cocotb tests** per module (FIFO, divider, CRC, coder, host SPI).
2. **Protocol tests:** each example program runs against an independent cocotb bus model (cocotbext-uart/spi/i2c,
   or simple hand-written models) at several rates/modes.
3. **Differential testing:** random instruction streams run on the RTL and on the ISS, comparing pins, FIFOs and registers every cycle.
4. **Formal (SymbiYosys):** FIFO invariants, host-SPI framing, no X on outputs after reset, and the
   repeat-without-consume invariant (no data-bit loss or duplication through the coder path).
5. **Gate-level simulation** of the hardened netlist (the template's `GATES=yes` flow).

## 7. Area budget *(est — to be replaced by real synthesis numbers)*

| Block | Flops | Notes |
|---|---|---|
| imem 64×16 | 1024 | + 4 × 64:1 × 16 read muxes. Candidate to move into an IHP SRAM macro |
| 4 × FIFOs (2 × 4 × 32) | 1024 | could shrink to 2-deep if area-tight |
| 4 × engine regs (X, Y, OSR, ISR, PC, divider, …) | ~720 | |
| 4 × CRC (LFSR + poly) | 256 | poly regs could be shared |
| host IF, coders, timestamp, GPIO | ~300 | |
| **total** | **~3.3 k** | ~25 % of 8x4 area *(est)* |

The first real synthesis run in week 2 decides FIFO depth, imem size, and whether to use flops or SRAM.
