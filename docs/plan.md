# Plan — deadline 2027-01-18

| Week | Dates | Milestone |
|---|---|---|
| 1 | Nov 17–23 | Spec frozen. Host SPI, imem, **one engine** (no helpers). Python assembler + ISS. `uart_tx` and `uart_rx` pass in cocotb. |
| 2 | Nov 24–30 | 4 engines, FIFOs, GPIO crossbar, open-drain. SPI controller (modes 0–3) + I2C controller. **First GDS run on GitHub Actions → real area/timing numbers.** |
| 3 | Dec 1–7 | CRC/LFSR, line coder (stuffing/NRZI/Manchester), timestamps, DPLL. SPI target + I2C target. |
| 4 | Dec 8–14 | Stretch: low-speed USB (packet RX/TX + CRC), 10BASE-T TX/RX (Manchester + NLP). |
| 5 | Dec 15–21 | Verification push: RTL-vs-ISS differential fuzzing, SymbiYosys proofs, gate-level sim. |
| 6 | Dec 22–28 | Timing/area closure at 60 MHz. RP2350 MicroPython host driver. Bonus protocols (WS2812, 1-Wire, CAN, PS/2, JTAG). |
| 7 | Dec 29–Jan 4 | Write-up: architecture, ISA reference, verification report, AI-usage note. |
| 8–9 | Jan 5–18 | Buffer, polish, final precheck, make repo public, submit form. |

## Open decisions
- FIFO depth (4 vs 2) and imem in flops vs IHP SRAM macro: decided by the week-2 synthesis numbers.
- Whether to add a QSPI host mode for 10BASE-T throughput (host SPI at ~10 Mbit/s is marginal for full-rate streaming).

## Admin
- [ ] Sign up: https://docs.google.com/forms/d/e/1FAIpQLSeF7fq756MegxZRQxotBwUJYZx-cL9MrGjxV0z4uD_J0sADxQ/viewform
- [ ] Join the Tiny Tapeout Discord competitions channel: https://discord.gg/rMmn8kXJng
- [ ] Create a GitHub repo (can stay private until submission) so the GDS action can run
- [ ] Questions → asic-competition@janestreet.com
