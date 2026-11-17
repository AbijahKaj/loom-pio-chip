# Loom ISA reference

Every instruction is 16 bits. It executes in exactly **one engine tick**, plus its delay. A tick is one
system clock divided by the engine's clock divider.

```
 15  13 12     9 8                     0
┌─────┬────────┬────────────────────────┐
│ op  │ dly/ss │        operands        │
└─────┴────────┴────────────────────────┘
```

**`dly/ss` (4 bits)** is shared between side-set and delay. The top `sideset_count` bits are side-set,
and the rest are the delay (0–15 ticks with no side-set). With `.side_set N opt`, the top bit says whether the
instruction drives side-set at all.

Side-set writes `sideset_count` pins starting at `sideset_base`, to pin values or pin directions. It takes
effect even when the instruction stalls, and it has priority over the instruction's own pin writes.
The delay starts only after the instruction completes. A stalled instruction adds no delay.

## Instructions

| op | Syntax | Semantics |
|---|---|---|
| 0 | `jmp [cond,] target` | Jump if `cond`. Conditions: *(always)*, `!x`, `x--` (jump if X≠0, then always decrement), `!y`, `y--`, `x!=y`, `pin` (GPIO `jmp_pin`), `!osre` (OSR not empty, i.e. shift count < pull threshold). |
| 1 | `wait pol src idx` | Stall until the source equals `pol`. Sources: `gpio N` (absolute GPIO), `pin N` (relative to `in_base`), `irq N` (shared flag; `wait 1 irq` clears it on success), `event E` (see Events). |
| 2 | `in src, n` | Shift `n` bits (1–32) of `src` into the ISR. Sources: `pins` (from `in_base`), `x`, `y`, `null`, `tstamp` (16-bit edge interval), `crc`, `isr`, `osr`. With autopush, pushes to the RX FIFO when the count reaches the threshold; stalls if the FIFO is full. |
| 2 | `in.c pins, n` / `in.c pins, x` | **Coded receive**: one line symbol per execution, decoded through NRZI/Manchester and de-stuffing, then into the ISR and the CRC. Repeats in place until `n` data bits have arrived (`n` = 0 or `x`: X-counted, see below). |
| 2 | `in.c null, n` | Decode and discard (e.g. a SYNC field). Line decoding and de-stuffing still run; nothing goes to the ISR or the CRC. |
| 3 | `out dst, n` | Shift `n` bits out of the OSR into `dst`: `pins` (`out_count` pins from `out_base`), `x`, `y`, `null`, `pindirs`, `pc` (computed jump), `isr` (sets the ISR count to n). Autopull refills an exhausted OSR in the same cycle, or stalls. |
| 3 | `out.c pins, n` / `out.c pins, x` | **Coded transmit**: one line symbol per execution, through stuffing and NRZI/Manchester (optionally driving a differential pair). Data bits feed the CRC. Repeats in place until `n` data bits have been sent. |
| 3 | `out.c null, n` | Feed `n` OSR bits through the CRC only, one bit per execution. |
| 4 | `push [iffull] [block\|noblock]` | ISR → RX FIFO and clear the ISR. `iffull`: only if the count ≥ threshold. `noblock` on a full FIFO drops the data. |
| 4 | `pull [ifempty] [block\|noblock]` | TX FIFO → OSR. `noblock` on an empty FIFO copies X. |
| 5 | `mov dst, [op]src` | `op`: none, `~` (invert), `::` (bit-reverse), `++` (increment). `dst`: `pins`, `x`, `y`, `crc`, `pindirs`, `pc`, `isr`, `osr`. `src`: `pins`, `x`, `y`, `null`, `status`, `crc`, `isr`, `osr`. |
| 6 | `irq [set\|wait\|clear] N [rel]` | 8 shared flags. `wait` sets the flag, then stalls until someone clears it. `rel` adds the engine number to bits [1:0]. |
| 7 | `set dst, imm6` | `pins` / `pindirs` (`set_count` pins from `set_base`), `x`, `y`. |
| 7 | `helper flags…` | The SET HELPER port: `crc` (reset the CRC to its init value and re-load the skip count), `coder` (reset the stuff run, NRZI level and Manchester phase), `clear` (clear the stuff-error and edge flags), `arm` (re-arm the event trap), `stuff_on` / `stuff_off`. |
| – | `nop` | Assembles to `mov y, y`. |

`status` (as a MOV source) = `{since_edge[15:0], 2'b0, edge_flag, stuff_err, osr_cnt[5:0], isr_cnt[5:0]}`.

## Coded ops: the repeat-without-consume rule

A coded op moves one **line symbol** per execution. If that symbol carries no data, the instruction
**re-executes without consuming or producing a data bit**. There are two such cases:

* **Stuffing.** After `stuff_n` identical bits, transmit emits a stuff bit and leaves the OSR untouched. Receive
  checks and discards the stuffed bit; a wrong value sets `stuff_err`.
  * Mode 0 (USB, HDLC): count ones, insert a 0.
  * Mode 1 (CAN): count equal bits, insert the complement.
* **Manchester.** Each data bit is two half-bit symbols. The first half always repeats.

**Completion.** A coded op completes after its last data bit, unless that bit leaves a stuff bit pending. In that case
it runs once more to send or discard the stuff bit, so end-of-packet stuffing is exact for USB, HDLC and CAN.

**X-counted mode** (`n` = 0, written `x`). Each data bit decrements X, and the op completes when X reaches 0.
One instruction can therefore stream a whole variable-length packet with no gaps between words. X = 0 at the
start means "unbounded" (2³² bits); such a receive usually ends through an event trap.

## Events

| # | Name | Meaning |
|---|---|---|
| 0 | `pattern` | `(gpio & PATMASK) == (PATVAL & PATMASK)` (e.g. USB SE0, SPI CS released) |
| 1 | `stufferr` | Sticky: a received stuff bit had the wrong value (e.g. USB EOP / HDLC abort) |
| 2 | `crc` | `(CRC ^ Y) & mask == 0`: the CRC residue equals Y |
| 3 | `timeout` | More than `TIMEOUT` cycles since the last edge on `ts_pin` |
| 4 | `edge` | Sticky: an edge was seen on `ts_pin` |
| 5 | `txempty` | TX FIFO empty |
| 6 | `rxfull` | RX FIFO full |
| 7 | `always` | 1 |

**Traps.** `TRAPCFG` names an event, a polarity and an address. While the trap is armed, the event at a tick
forces `pc = trap_addr` instead of executing, and cancels any delay or in-progress coded op. The trap then disarms
until it is re-armed with `helper arm`, a restart, or a host write.

## Timing summary

* Pin outputs change on the clock edge at which the instruction executes.
* Inputs pass through a 2-flop synchroniser, so pins are sampled 2 cycles late.
* `jmp`, `wait`, `set` and `mov` all take one tick, so loops can be counted exactly.
* The clock divider is 16.8 fixed point; DPLL mode re-phases it to mid-bit on every `ts_pin` edge.
