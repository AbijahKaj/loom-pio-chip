# Plan and status (deadline 2027-01-18)

## Done

- [x] RTL: 4 bit engines with line helpers, host SPI target with quad-write mode, FIFOs, GPIO/open-drain, IRQs
- [x] Toolchain: assembler + CLI (`tools/loomasm`), disassembler, cycle-exact ISS, register encoder, export tool
- [x] 16 protocol programs (UART, SPI ×4 modes, SPI target, I2C, USB LS TX/RX, 10BASE-T TX + NLP, Manchester RX, CAN TX/RX, WS2812, PS/2, pulse capture)
- [x] Verification: RTL-vs-ISS lockstep with coverage; 22 protocol scenarios against independent models (ISS + RTL);
      host driver run against the RTL; formal (FIFO proof, engine BMC); toolchain unit tests (7 catalogue CRCs);
      post-synthesis gate-level simulation
- [x] Docs: architecture, ISA, verification, hardening, datasheet page
- [x] Area: ~459k µm² post-synthesis (`AREA 0`), 32-entry imem for routability

## Remaining

- [x] Hardened on the Linux build machine (Nix, `synth/remote_harden.sh`): **run 6 signed off**: DRC/LVS/antenna
      clean, timing met at all corners (30 MHz worst case, ~52 MHz typical), **TT precheck passed**
- [x] Official gate-level test on the routed netlist: 25/25
- [x] Final PnR numbers in `docs/architecture.md` §6 and `docs/hardening.md`
- [x] AI-usage section written (docs/verification.md §6); edit if you want different wording
- [ ] Optional: I2C target program; an RP2350 PIO-based SPI for faster host access; demo video.
- [ ] Make the repository public and submit the form (link to be published on the competition page).
