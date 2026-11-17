# Verification

Loom is checked in four independent layers. Each one catches a different class of bug.

| Layer | What it checks | Where |
|---|---|---|
| 1. Golden model + lockstep diff | Every architectural register of every engine, the GPIO/OE outputs and the IRQ flags, compared **every clock cycle** between the RTL and the Python ISS, over random programs, configurations, pin stimulus and host traffic | `tools/loom/sim.py`, `test/test_diff.py` |
| 2. Protocol scenarios | 17 end-to-end protocol tests against **independent** device models (written from the protocol specifications, sharing no code with the engine or its programs), run on both the ISS and the RTL, with all host accesses through the real SPI interface | `test/scenarios.py`, `test/models.py`, `test/test_protocols.py`, `test/run_iss.py` |
| 3. Formal | k-induction proof of the FIFO against a reference queue; bounded model check of the engine's core invariants from reset | `formal/`, assertions under `` `ifdef FORMAL `` in `src/loom_engine.v` |
| 4. Gate level | The smoke test (SPI register access, imem load, UART TX) on the hardened netlist | `test/test.py` with `GATES=yes` |

## 1. Golden model and lockstep differential testing

`tools/loom/sim.py` is a cycle-exact model of everything behind the SPI block. It is written as a mirror of the RTL:
a combinational next-state step, then the sequential overrides in the same order. It is written in plain Python
with no HDL semantics, so a shared misunderstanding has to be made twice, in two different languages, to slip
through.

`test/test_diff.py` runs both models in lockstep:

* The RTL's internal host strobes (`h_wr`, `h_wr_addr`, `h_wdata`, `h_rd_pop`) are sampled each cycle and fed to the
  ISS, so both see host writes at exactly the same edge.
* Random **programs** are biased towards interesting code: coded serial ops (including X-counted), non-blocking
  push/pull, waits on events, IRQs and computed jumps. Random **configurations** cover every field: dividers with
  fractions, side-set variants, shift directions, auto push/pull thresholds, every coder mode, stuff lengths 1–15,
  CRC widths 1–32, skip counts, traps, DPLL. **Pins** are randomised every cycle, and random **host traffic**
  (FIFO writes and reads, IRQ writes, restarts, FIFO clears, divider syncs, GPIO writes, coder reconfiguration
  mid-run) runs concurrently.
* After every edge, 25 state fields per engine, both FIFO levels, `gpio_out`, `gpio_oe` and the IRQ flags are compared.

**Coverage.** The ISS counts functional coverage points: each opcode executed and stalled, coded receive/transmit
in every coder mode, coded repeats, stuff insertion, stuff errors, trap firings, autopush/autopull, CRC steps,
side-set, jumps and timestamp edges. The test fails if any required point is never hit, which caught the first
version of the generator never exercising bit-stuffing.

Typical run: 16 seeds × ~35k cycles, about 560k compared cycles, all coverage points hit.

## 2. Protocol scenarios

Each scenario configures engines exactly as an application would and checks the result with an independent model:

| Scenario | Checked against |
|---|---|
| UART TX + RX at 500 kbaud, framing-error IRQ | `UartMonitor` / `UartDriver` |
| SPI controller, modes 0, 1, 2, 3 (full duplex) | `SpiTarget` (any CPOL/CPHA) |
| SPI target with CS-abort trap | `SpiController` (incl. a transfer aborted mid-byte) |
| I2C controller: write, repeated-start read, NACK; with and without clock stretching | `I2CTarget` (24Cxx-style register memory, open drain, wired-AND bus) |
| USB low-speed TX: DATA0 setup packet, stuffing-heavy packet, handshake | `UsbLsMonitor` (NRZI decode, de-stuff, EOP) and a reference CRC16 |
| USB low-speed RX, incl. **1.5% clock error** and a corrupted packet | `UsbLsDriver`; the hardware CRC residue must be 0x800D for good packets and differ for the corrupted one |
| 10BASE-T TX at full rate (quad-SPI host feed) | Offline Manchester decoder and `zlib.crc32` FCS; differential pair must stay complementary |
| CAN 2.0 TX | Reference CAN stuffer (incl. trailing stuff bit) and CRC15 |
| WS2812 | Pulse-width monitor (T0H / T1H in spec) |
| PS/2 RX | `PS2Device`, incl. parity and stop bits |
| Pulse capture | `PulseGen` with known intervals |
| Four engines concurrently (UART TX, UART RX, SPI, pulse capture) | All of the above at once |

The same scenario code runs on both backends (`test/board.py`). The ISS backend is ~100× faster and is used to
develop programs. The cocotb backend drives the RTL through the real bit-banged SPI host, so it also exercises the
SPI block, read-ahead/pop semantics, quad writes and realistic host latency.

## 3. Formal

* **FIFO (unbounded proof):** `loom_fifo` is compared with a 2-entry reference queue. Level, empty, full and head
  data are proven equal at all times by k-induction (`sat -tempinduct`, succeeds at depth 3).
* **Engine (bounded, 14 cycles from reset, all inputs free: instructions, pins, config writes, FIFO traffic):**
  * a stuff symbol or Manchester first half never consumes OSR data, never touches X or the CRC, and a Manchester
    first half never advances the PC (*the repeat-without-consume rule*);
  * a discarded received stuff bit never reaches the ISR, the CRC or the RX FIFO;
  * every consumed coded data bit advances the OSR count by exactly one;
  * a stalled instruction changes no architectural state and adds no delay;
  * FIFOs are never pushed when full or popped when empty;
  * nothing executes or drives pins without a tick;
  * shift counters never exceed 32.

Run with `formal/run.sh` (plain Yosys, no SymbiYosys needed).

## 4. Bugs found (and what found them)

| # | Bug | Found by |
|---|---|---|
| 1 | Writing enable+restart in one CTRL write let the engine execute (and pop a FIFO word) in the same cycle the restart overrode its state, losing the first UART byte. Fixed by suppressing ticks during restart. | ISS + UART check, before any RTL test |
| 2 | Reading an empty RX FIFO returned uninitialised RAM (X on MISO in simulation). Now returns 0. | Lockstep test (X propagation) |
| 3 | The CAN program spent one bit time on a `helper` instruction between the CRC and its delimiter. | CAN reference model |
| 4 | Host bandwidth: single-bit SPI cannot feed 10BASE-T. Added quad-write mode. | RTL protocol test with the real SPI host |
| 5 | `quad` register declared after use (accepted by Verilator, rejected by Icarus). | Multi-simulator flow |
| 6 | An over-strict formal assertion: an X-counted transmit *does* complete on a trailing stuff bit, by design. The assertion was refined. | BMC counterexample |
| 7 | Synthesis with `DELAY 4` inserted ~34k buffers (947k µm²); first PnR failed detailed placement. Switched to `AREA 0`. | Local LibreLane run |
| 8 | Routing congestion on Metal3 with a 64×16, 4-port imem. Reduced to 32 entries; density/derating tuned. | Local LibreLane global-routing report |
| 9 | Test-harness bugs (cocotb read-after-write in one timestep; a model treating a reset-low line as a start bit; quad data pins overwritten by the pin resolver). | Debugging failing scenarios: the bugs were in the checkers, not the design |

## 5. How to run

```sh
# RTL: smoke + all protocol scenarios + lockstep diff (needs icarus, cocotb in .venv)
cd test && make -B NODUMP=1 COCOTB_TEST_MODULES=test,test_protocols,test_diff
# protocol scenarios on the ISS only (seconds)
python3 test/run_iss.py
# formal
formal/run.sh
# area estimate / full hardening
synth/area.sh ; synth/harden.sh
```

## 6. Tools and AI assistance

*(Entrants are asked to describe their use of AI tools. Edit this section to reflect your own process.)*

An AI coding assistant was used during development to help write RTL, the Python toolchain, tests and documentation,
and to run and debug the simulation and hardening flows. Every design decision was reviewed by the author, and all
claims in this document are backed by the tests and logs in the repository.
