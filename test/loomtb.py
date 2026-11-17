"""Shared cocotb harness for Loom: SPI host driver, program loading, pin access."""
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from loom import regs as R  # noqa: E402
from loom.asm import assemble_file  # noqa: E402

PROG_DIR = os.path.join(os.path.dirname(__file__), "..", "programs")
CLK_NS = 16          # ~60 MHz
SPI_HALF = 5         # SCK half period in clk cycles (SCK = clk/10)
GATES = os.environ.get("GATES") == "yes"


def prog(name, **defines):
    return assemble_file(os.path.join(PROG_DIR, name + ".loom"), defines)


class Loom:
    def __init__(self, dut):
        self.dut = dut
        self.ui = 0x02           # CS_N high
        self.uio_drive = 0       # value the testbench drives on uio pins
        self.uio_tb_oe = 0       # which uio pins the testbench drives (else pulled/high-Z)
        self.uio_pullup = 0      # pins with an external pull-up (I2C, 1-Wire)

    # ------------------------------------------------------------ setup
    async def start(self):
        dut = self.dut
        cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
        dut.ena.value = 1
        self._apply()
        dut.rst_n.value = 0
        await ClockCycles(dut.clk, 5)
        dut.rst_n.value = 1
        await ClockCycles(dut.clk, 3)
        cocotb.start_soon(self._uio_resolver())

    def _apply(self):
        self.dut.ui_in.value = self.ui & 0xFF
        self._resolve_uio()

    def _resolve_uio(self):
        """Wired-AND style resolution of the bidirectional pins: pulled-up lines are
        low if anyone drives low; otherwise the active driver wins."""
        try:
            oe = int(self.dut.uio_oe.value)
            out = int(self.dut.uio_out.value)
        except ValueError:
            oe, out = 0, 0
        v = 0
        for i in range(8):
            b = 1 << i
            chip_low = (oe & b) and not (out & b)
            chip_high = (oe & b) and (out & b)
            tb_low = (self.uio_tb_oe & b) and not (self.uio_drive & b)
            tb_high = (self.uio_tb_oe & b) and (self.uio_drive & b)
            if self.uio_pullup & b:
                bit = 0 if (chip_low or tb_low) else 1
            elif chip_high or chip_low:
                bit = 1 if chip_high else 0
            else:
                bit = 1 if tb_high else 0
            v |= bit << i if bit else 0
        self.dut.uio_in.value = v

    async def _uio_resolver(self):
        while True:
            await FallingEdge(self.dut.clk)
            self._resolve_uio()

    # ------------------------------------------------------------ pins
    def set_gpio_in(self, gpio, val):
        """Drive an input-only GPIO (8..12 -> ui[3..7]) or a uio pin (0..7)."""
        if 8 <= gpio <= 12:
            b = 1 << (gpio - 5)
            self.ui = (self.ui | b) if val else (self.ui & ~b)
            self.dut.ui_in.value = self.ui
        elif 0 <= gpio <= 7:
            b = 1 << gpio
            self.uio_tb_oe |= b
            self.uio_drive = (self.uio_drive | b) if val else (self.uio_drive & ~b)
            self._resolve_uio()
        else:
            raise ValueError(gpio)

    def release_gpio(self, gpio):
        self.uio_tb_oe &= ~(1 << gpio)
        self._resolve_uio()

    def gpio(self, n):
        """Current level of GPIO n as seen on the pads."""
        if 13 <= n <= 19:
            return (int(self.dut.uo_out.value) >> (n - 12)) & 1
        if 8 <= n <= 12:
            return (self.ui >> (n - 5)) & 1
        self._resolve_uio()
        return (int(self.dut.uio_in.value) >> n) & 1

    # ------------------------------------------------------------ SPI host
    async def _spi_byte(self, out_byte):
        dut = self.dut
        inb = 0
        for i in range(7, -1, -1):
            self.ui = (self.ui & ~0x04) | (((out_byte >> i) & 1) << 2)
            dut.ui_in.value = self.ui
            await ClockCycles(dut.clk, SPI_HALF)
            inb = (inb << 1) | (int(dut.uo_out.value) & 1)
            self.ui |= 0x01
            dut.ui_in.value = self.ui
            await ClockCycles(dut.clk, SPI_HALF)
            self.ui &= ~0x01
            dut.ui_in.value = self.ui
        return inb

    async def _spi_byte_quad(self, out_byte):
        dut = self.dut
        for nib in (out_byte >> 4, out_byte & 15):
            # qio[0] = MOSI (ui[2]), qio[3:1] = ui[5:3]
            self.ui = (self.ui & ~0x3C) | ((nib & 1) << 2) | ((nib >> 1) << 3)
            dut.ui_in.value = self.ui
            await ClockCycles(dut.clk, SPI_HALF)
            self.ui |= 0x01
            dut.ui_in.value = self.ui
            await ClockCycles(dut.clk, SPI_HALF)
            self.ui &= ~0x01
            dut.ui_in.value = self.ui

    async def _cs(self, low):
        self.ui = (self.ui & ~0x02) | (0 if low else 0x02)
        self.dut.ui_in.value = self.ui
        await ClockCycles(self.dut.clk, SPI_HALF)

    quad = False

    async def write(self, addr, *words):
        await self._cs(True)
        await self._spi_byte(addr & 0x7F)
        for w in words:
            for k in range(4):
                if self.quad:
                    await self._spi_byte_quad((w >> (8 * k)) & 0xFF)
                else:
                    await self._spi_byte((w >> (8 * k)) & 0xFF)
        await ClockCycles(self.dut.clk, 3)
        await self._cs(False)
        await ClockCycles(self.dut.clk, 4)

    async def read(self, addr, n=1):
        await self._cs(True)
        await self._spi_byte(0x80 | (addr & 0x7F))
        out = []
        for _ in range(n):
            w = 0
            for k in range(4):
                w |= (await self._spi_byte(0)) << (8 * k)
            out.append(w)
        await self._cs(False)
        await ClockCycles(self.dut.clk, 4)
        return out if n > 1 else out[0]

    # ------------------------------------------------------------ high level
    async def load(self, program, origin=0, engine=0):
        await self.write(R.IMEM_ADDR, (engine << 6) | origin)
        await self.write(R.IMEM_DATA, *program.relocated(origin))

    async def configure(self, n, cfg):
        for r, v in cfg.regs().items():
            await self.write(R.ENG(n, r), v)
        await self.write(R.ENG(n, R.CRCVAL), cfg.crc_init_value())

    async def enable(self, mask, restart=None):
        restart = mask if restart is None else restart
        await self.write(R.CTRL, (mask & 15) | ((restart & 15) << 4))

    async def push(self, n, *words):
        await self.write(R.ENG(n, R.TXF), *words)

    async def levels(self, n):
        v = await self.read(R.FLEVEL)
        return (v >> (8 * n)) & 7, (v >> (8 * n + 4)) & 7   # (tx, rx)

    async def pop_all(self, n, timeout_words=64):
        out = []
        for _ in range(timeout_words):
            tx, rx = await self.levels(n)
            if rx == 0:
                break
            out += [await self.read(R.ENG(n, R.RXF))] if rx == 1 else await self.read(R.ENG(n, R.RXF), rx)
        return out

    async def wait_rx(self, n, count, max_cycles=200000):
        """Collect `count` words from engine n's RX FIFO, polling over SPI."""
        got = []
        cycles = 0
        while len(got) < count and cycles < max_cycles:
            tx, rx = await self.levels(n)
            if rx:
                w = await self.read(R.ENG(n, R.RXF), rx)
                got += w if isinstance(w, list) else [w]
            else:
                await ClockCycles(self.dut.clk, 50)
                cycles += 50
            cycles += 200
        return got


async def capture(dut, sample_fn, cycles):
    """Sample a value every clock (at the falling edge) for `cycles` cycles."""
    out = []
    for _ in range(cycles):
        await FallingEdge(dut.clk)
        out.append(sample_fn())
    return out
