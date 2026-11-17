"""Loom host register map and configuration-word encoders.

Shared by the cocotb tests, the ISS and (in a MicroPython-friendly subset)
the RP2350 host driver in host/loom.py.
"""
from dataclasses import dataclass, field

# ---------------------------------------------------------------- global registers
ID = 0x00          # R   "LOOM" = 0x4C4F4F4D
CTRL = 0x01        # RW  [3:0] enable, W1P: [7:4] restart, [11:8] fifo clear, [15:12] divider sync
FLEVEL = 0x02      # R   per engine n: [8n+2:8n] TX level, [8n+6:8n+4] RX level
IMEM_ADDR = 0x03   # RW  imem write pointer
IMEM_DATA = 0x05   # W   imem[ptr++] = data[15:0]   (port: address does not auto-increment)
IRQ = 0x06         # RW  R: flags; W: clear [7:0], set [15:8]
GPIO = 0x07        # RW  R: 20-bit input vector; W: GPIO output register
OD = 0x08          # RW  open-drain enables for GPIO0..7
OE = 0x09          # RW  output enables for GPIO0..7 (non open-drain pins)

LOOM_ID = 0x4C4F4F4D

# ---------------------------------------------------------------- engine registers
CLKDIV, EXECCFG, SHIFTCFG, PINCFG, TXF, RXF, CODECFG, CRCCFG, CRCPOLY, CRCVAL = range(10)
PATMASK, PATVAL, TRAPCFG, DBG, DBGX = 0xB, 0xC, 0xD, 0xE, 0xF


def ENG(n: int, reg: int) -> int:
    """Address of engine *n*'s register *reg*."""
    return 0x10 * (n + 1) + reg


def is_port(addr: int) -> bool:
    return addr == IMEM_DATA or (addr >= 0x10 and (addr & 0xF) in (TXF, RXF))


# coder modes
CODE_NONE, CODE_NRZI, CODE_MANCH = 0, 1, 2
# timestamp edge selection
EDGE_RISE, EDGE_FALL, EDGE_BOTH = 1, 2, 3
# events
EV_PATTERN, EV_STUFF_ERR, EV_CRC_EQ_Y, EV_TIMEOUT, EV_EDGE, EV_TXF_EMPTY, EV_RXF_FULL, EV_ALWAYS = range(8)
EVENT_NAMES = {
    "pattern": EV_PATTERN, "stufferr": EV_STUFF_ERR, "crc": EV_CRC_EQ_Y, "timeout": EV_TIMEOUT,
    "edge": EV_EDGE, "txempty": EV_TXF_EMPTY, "rxfull": EV_RXF_FULL, "always": EV_ALWAYS,
}

# GPIO numbering helpers (physical pin -> GPIO index)
def UIO(i): return i            # bidirectional uio[i]
def UI(i): return 5 + i          # ui[3..7] -> GPIO 8..12
def UO(i): return 12 + i         # uo[1..7] -> GPIO 13..19


@dataclass
class EngineConfig:
    clkdiv: float = 1.0
    wrap_bottom: int = 0
    wrap_top: int = 63
    start: int = 0
    sideset_count: int = 0       # total side-set bits incl. the optional enable bit
    sideset_opt: bool = False
    sideset_pindirs: bool = False
    jmp_pin: int = 0
    in_shift_right: bool = False
    out_shift_right: bool = False
    autopush: bool = False
    autopull: bool = False
    push_thresh: int = 32
    pull_thresh: int = 32
    out_base: int = 0
    out_count: int = 0
    set_base: int = 0
    set_count: int = 0
    sideset_base: int = 0
    in_base: int = 0
    # line coder
    code_mode: int = CODE_NONE
    nrzi_toggle_on: int = 0      # data value that causes a line transition (USB: 0)
    manch_pol: int = 0           # 0: IEEE 802.3 ('1' = low->high), 1: G.E. Thomas
    stuff_en: bool = False
    stuff_mode: int = 0          # 0: count ones, insert 0 (USB/HDLC); 1: count equal bits, insert complement (CAN)
    stuff_n: int = 0
    diff: bool = False           # coded output also drives out_base+1 with the complement
    idle_level: int = 0
    ts_pin: int = 0
    ts_edge: int = 0
    dpll: bool = False
    # CRC
    crc_width: int = 32
    crc_init_ones: bool = False
    crc_poly: int = 0
    crc_skip: int = 0            # data bits excluded from the CRC after each CRC reset
    # events / traps
    pat_mask: int = 0
    pat_val: int = 0
    trap_en: bool = False
    trap_event: int = 0
    trap_pol: int = 1
    trap_addr: int = 0
    timeout: int = 0xFFFF

    def clkdiv_word(self) -> int:
        i = int(self.clkdiv)
        f = int(round((self.clkdiv - i) * 256))
        if f == 256:
            i, f = i + 1, 0
        return ((i & 0xFFFF) << 16) | ((f & 0xFF) << 8)

    def regs(self) -> dict:
        r = {}
        r[CLKDIV] = self.clkdiv_word()
        r[EXECCFG] = (self.wrap_bottom & 63) | (self.wrap_top & 63) << 6 | (self.start & 63) << 12 \
            | (self.sideset_count & 7) << 18 | int(self.sideset_opt) << 21 | int(self.sideset_pindirs) << 22 \
            | (self.jmp_pin & 31) << 23
        r[SHIFTCFG] = int(self.in_shift_right) | int(self.out_shift_right) << 1 | int(self.autopush) << 2 \
            | int(self.autopull) << 3 | (self.push_thresh & 31) << 4 | (self.pull_thresh & 31) << 9
        r[PINCFG] = (self.out_base & 31) | (self.out_count & 63) << 5 | (self.set_base & 31) << 11 \
            | (self.set_count & 7) << 16 | (self.sideset_base & 31) << 19 | (self.in_base & 31) << 24
        r[CODECFG] = (self.code_mode & 3) | (self.nrzi_toggle_on & 1) << 2 | (self.manch_pol & 1) << 3 \
            | int(self.stuff_en) << 4 | (self.stuff_mode & 1) << 5 | (self.stuff_n & 15) << 6 \
            | int(self.diff) << 10 | (self.idle_level & 1) << 11 | (self.ts_pin & 31) << 12 \
            | (self.ts_edge & 3) << 17 | int(self.dpll) << 19
        r[CRCCFG] = (self.crc_width & 63) | int(self.crc_init_ones) << 6 | (self.crc_skip & 63) << 7
        r[CRCPOLY] = self.crc_poly & 0xFFFFFFFF
        r[PATMASK] = self.pat_mask & 0xFFFFF
        r[PATVAL] = self.pat_val & 0xFFFFF
        r[TRAPCFG] = int(self.trap_en) | (self.trap_event & 7) << 1 | (self.trap_pol & 1) << 4 \
            | (self.trap_addr & 63) << 5 | (self.timeout & 0xFFFF) << 16
        return r

    def crc_init_value(self) -> int:
        w = self.crc_width if 0 < self.crc_width <= 32 else 32
        return ((1 << w) - 1) if self.crc_init_ones else 0
