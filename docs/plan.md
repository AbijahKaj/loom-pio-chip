# Plan and status (deadline 2027-01-18)

## Done

- [x] RTL: 4 bit engines with line helpers, host SPI target with quad-write mode, FIFOs, GPIO/open-drain, IRQs
- [x] Toolchain: assembler + CLI (`tools/loomasm`), disassembler, cycle-exact ISS, register encoder, export tool
- [x] 14 protocol programs (UART, SPI ×4 modes, SPI target, I2C, USB LS TX/RX, 10BASE-T TX + NLP, CAN TX, WS2812, PS/2, pulse capture)
- [x] Verification: RTL-vs-ISS lockstep with coverage; 17 protocol scenarios against independent models (ISS + RTL);
      host driver run against the RTL; formal (FIFO proof, engine BMC); toolchain unit tests (7 catalogue CRCs);
      post-synthesis gate-level simulation
- [x] Docs: architecture, ISA, verification, hardening, datasheet page
- [x] Area: ~459k µm² post-synthesis (`AREA 0`), 32-entry imem for routability

## Remaining

- [ ] **Harden on the Linux build machine** (`synth/harden.sh`; Docker or Nix needed there), check routing DRC = 0,
      timing at 16 ns, TT precheck. If routing still congests: reduce the side-set/pin rotators or the FIFO read
      muxes next.
- [ ] Official gate-level test on the PnR netlist (`make -B GATES=yes`).
- [ ] Update `docs/architecture.md` §6 and `docs/hardening.md` with the final PnR numbers.
- [ ] Edit the AI-usage paragraph in `docs/verification.md` §6 to describe your own process.
- [ ] Optional: CAN RX and I2C target programs; an RP2350 PIO-based SPI for faster host access; demo video.
- [ ] Make the repository public and submit the form (link to be published on the competition page).
