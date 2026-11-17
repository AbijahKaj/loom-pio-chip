"""Loom host driver for MicroPython (RP2350 on the Tiny Tapeout demo board) or CPython.

The SPI link is bit-banged on four pins (mode 0), so any GPIOs can be used. Pass
objects with .value() (machine.Pin) for SCK, CS_N and MOSI (outputs) and MISO (input).
On the TT demo board these are the RP2350 pins wired to the project's
ui_in[0] (SCK), ui_in[1] (CS_N), ui_in[2] (MOSI) and uo_out[0] (MISO).

Programs and engine configurations are prepared on a PC with
`python3 tools/loom/export.py programs/uart_tx.loom ... > uart_demo.py`
and imported here as plain data (no assembler needed on the board).
"""

ID, CTRL, FLEVEL, IMEM_ADDR, IMEM_DATA, IRQ, GPIO, OD, OE, HOSTCFG = 0, 1, 2, 3, 5, 6, 7, 8, 9, 10
CLKDIV, EXECCFG, SHIFTCFG, PINCFG, TXF, RXF, CODECFG, CRCCFG, CRCPOLY, CRCVAL = range(10)
PATMASK, PATVAL, TRAPCFG, DBG, DBGX = 0xB, 0xC, 0xD, 0xE, 0xF
LOOM_ID = 0x4C4F4F4D


def eng(n, reg):
    return 0x10 * (n + 1) + reg


class Loom:
    def __init__(self, sck, cs_n, mosi, miso, io=None):
        """io: optional (io1, io2, io3) pins = GPIO8..10 for quad writes."""
        self.sck, self.cs, self.mosi, self.miso, self.io = sck, cs_n, mosi, miso, io
        self.sck.value(0)
        self.cs.value(1)
        self.quad = False

    # ------------------------------------------------------------ raw SPI
    def _xfer(self, b):
        r = 0
        for i in range(7, -1, -1):
            self.mosi.value((b >> i) & 1)
            r = (r << 1) | self.miso.value()
            self.sck.value(1)
            self.sck.value(0)
        return r

    def _xfer_quad(self, b):
        for nib in (b >> 4, b & 15):
            self.mosi.value(nib & 1)
            self.io[0].value((nib >> 1) & 1)
            self.io[1].value((nib >> 2) & 1)
            self.io[2].value((nib >> 3) & 1)
            self.sck.value(1)
            self.sck.value(0)

    def write(self, addr, *words):
        self.cs.value(0)
        self._xfer(addr & 0x7F)
        put = self._xfer_quad if self.quad else self._xfer
        for w in words:
            for k in range(4):
                put((w >> (8 * k)) & 0xFF)
        self.cs.value(1)

    def read(self, addr, n=1):
        self.cs.value(0)
        self._xfer(0x80 | (addr & 0x7F))
        out = []
        for _ in range(n):
            w = 0
            for k in range(4):
                w |= self._xfer(0) << (8 * k)
            out.append(w)
        self.cs.value(1)
        return out if n > 1 else out[0]

    # ------------------------------------------------------------ helpers
    def check(self):
        v = self.read(ID)
        if v != LOOM_ID:
            raise OSError("Loom not found (ID=%08x)" % v)

    def load(self, words, origin=0, engine=0):
        """Load into an engine's 64-word bank (the engine must be disabled)."""
        self.write(IMEM_ADDR, (engine << 6) | origin)
        self.write(IMEM_DATA, *words)

    def configure(self, n, regs):
        """regs: {reg_offset: value} as produced by tools/loom/export.py."""
        for r in sorted(regs):
            self.write(eng(n, r), regs[r])

    def enable(self, mask, restart=None):
        restart = mask if restart is None else restart
        self.write(CTRL, (mask & 15) | ((restart & 15) << 4))

    def set_quad(self, on):
        if on and not self.io:
            raise ValueError("quad mode needs io pins")
        self.write(HOSTCFG, 1 if on else 0)   # this write itself is still single-bit when enabling
        self.quad = on

    def levels(self, n):
        v = self.read(FLEVEL)
        return (v >> (8 * n)) & 7, (v >> (8 * n + 4)) & 7

    def push(self, n, *words):
        """Push words, waiting for FIFO space between them."""
        for w in words:
            while self.levels(n)[0] >= 2:
                pass
            self.write(eng(n, TXF), w)

    def pop(self, n):
        out = []
        rx = self.levels(n)[1]
        if rx:
            r = self.read(eng(n, RXF), rx)
            out = r if isinstance(r, list) else [r]
        return out

    def run(self, app):
        """Load and start an exported application module (see tools/loom/export.py)."""
        self.check()
        for gpio_out in app.get("gpio", []):
            self.write(GPIO, gpio_out)
        if "od" in app:
            self.write(OD, app["od"])
        mask = 0
        for e in app["engines"]:
            self.load(e["code"], e["origin"], e["engine"])
            self.configure(e["engine"], e["regs"])
            mask |= 1 << e["engine"]
        self.enable(mask)
