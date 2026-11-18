"""Board-level harness: Loom + external device models, on two backends.

* IssBackend    -- the cycle-exact Python ISS (fast; used to develop programs)
* CocotbBackend -- the RTL in cocotb, with the real SPI host interface

Device models are synchronous per-cycle state machines (see models.py). Each cycle:
chip outputs + model drives are resolved into pad levels (wired-AND with optional
pull-ups on the bidirectional pins), the chip samples those levels, and every
model observes them and decides its drives for the next cycle.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from loom import regs as R  # noqa: E402
from loom.asm import assemble_file  # noqa: E402
from loom.sim import Chip  # noqa: E402

PROG_DIR = os.path.join(os.path.dirname(__file__), "..", "programs")


def prog(name, **defines):
    return assemble_file(os.path.join(PROG_DIR, name + ".loom"), defines)


class Board:
    def __init__(self, backend):
        self.be = backend
        self.models = []
        self.pullups = set()
        self.levels = [0] * 20
        self.cycle = 0
        self.rxbuf = {}            # RX words drained while waiting for TX space
        self.slow_host = getattr(backend, "slow_host", False)
        backend.board = self

    def add(self, model):
        self.models.append(model)
        return model

    # ------------------------------------------------------------ per-cycle resolution
    def resolve(self, uo, uio_out, uio_oe):
        drives = {}
        for m in self.models:
            for g, v in m.drives().items():
                if v is None:
                    continue
                drives[g] = min(drives.get(g, 1), v) if g in drives else v
        lv = [0] * 20
        for g in range(20):
            if g <= 7:
                chip_drv = (uio_out >> g) & 1 if (uio_oe >> g) & 1 else None
                ds = [d for d in (chip_drv, drives.get(g)) if d is not None]
                if ds:
                    lv[g] = min(ds)
                else:
                    lv[g] = 1 if g in self.pullups else self.levels[g]
            elif g <= 12:
                v = drives.get(g)
                lv[g] = (1 if g in self.pullups else 0) if v is None else v
            else:
                lv[g] = (uo >> (g - 12)) & 1
        self.levels = lv
        return lv

    def tick_models(self):
        for m in self.models:
            m.tick(self.cycle, self.levels)
        self.cycle += 1

    def ui_hi(self):
        return sum(self.levels[8 + i] << (3 + i) for i in range(5))

    def uio_in(self):
        return sum(self.levels[i] << i for i in range(8))

    # ------------------------------------------------------------ host API (delegated)
    async def cycles(self, n):
        await self.be.cycles(n)

    async def write(self, addr, *words):
        await self.be.write(addr, *words)

    async def read(self, addr, n=1):
        return await self.be.read(addr, n)

    async def write_quad(self, addr, *words):
        """Write using quad-SPI data (HOSTCFG.quad must be set)."""
        await self.be.write(addr, *words, quad=True)

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

    async def push_wait(self, n, *words, max_cycles=2_000_000):
        """Push words, waiting for TX FIFO space (FIFO depth is small)."""
        for w in words:
            waited = 0
            while True:
                tx, rx = await self.fifo_levels(n)
                if rx:   # keep the engine from stalling on a full RX FIFO
                    self.rxbuf.setdefault(n, []).extend(await self._read_rx(n, rx))
                if tx < 2:
                    break
                await self.cycles(20)
                waited += 20
                assert waited < max_cycles, "TX FIFO never drained"
            await self.push(n, w)

    async def fifo_levels(self, n):
        v = await self.read(R.FLEVEL)
        return (v >> (8 * n)) & 7, (v >> (8 * n + 4)) & 7

    async def _read_rx(self, n, count):
        w = await self.read(R.ENG(n, R.RXF), count)
        return w if isinstance(w, list) else [w]

    async def drain(self, n):
        out = self.rxbuf.pop(n, [])
        while True:
            _, rx = await self.fifo_levels(n)
            if not rx:
                return out
            out += await self._read_rx(n, rx)

    async def collect(self, n, count, max_cycles=1_000_000, poll=40):
        out = []
        start = self.cycle
        while len(out) < count:
            out += await self.drain(n)
            if len(out) >= count:
                break
            assert self.cycle - start < max_cycles, f"timeout: got {len(out)}/{count} words"
            await self.cycles(poll)
        return out

    async def wait_for(self, pred, max_cycles=1_000_000, step=10):
        start = self.cycle
        while not pred():
            assert self.cycle - start < max_cycles, "wait_for timeout"
            await self.cycles(step)

    async def irq(self):
        return await self.read(R.IRQ)

    async def clear_irq(self, mask):
        await self.write(R.IRQ, mask & 0xFF)


class IssBackend:
    """Runs the board on the Python ISS. Host accesses take one cycle each."""

    def __init__(self):
        self.chip = Chip()
        self.board = None

    def _step(self, wr=None, rd_pop=None):
        b = self.board
        uo, uio_out, uio_oe = self.chip.pads()
        b.resolve(uo << 1, uio_out, uio_oe)
        self.chip.step(ui_in=b.ui_hi() | 0x02, uio_in=b.uio_in(), wr=wr, rd_pop=rd_pop)
        b.tick_models()

    async def cycles(self, n):
        for _ in range(n):
            self._step()

    async def write(self, addr, *words, quad=False):
        for i, w in enumerate(words):
            a = addr if R.is_port(addr) else addr + i
            self._step(wr=(a, w & 0xFFFFFFFF))

    async def read(self, addr, n=1):
        out = []
        for i in range(n):
            a = addr if R.is_port(addr) else addr + i
            d, v = self.chip.read(a)
            self._step(rd_pop=a if (v and (a & 0xF) == R.RXF and a >= 0x10) else None)
            out.append(d)
        return out if n > 1 else out[0]


class CocotbBackend:
    """Runs the board on the RTL through the real SPI host interface."""

    def __init__(self, dut):
        import loomtb
        self.dut = dut
        self.lm = loomtb.Loom(dut)
        self.board = None

    async def start(self):
        import cocotb
        from cocotb.clock import Clock
        from cocotb.triggers import ClockCycles
        import loomtb
        dut = self.dut
        cocotb.start_soon(Clock(dut.clk, loomtb.CLK_NS, unit="ns").start())
        dut.ena.value = 1
        dut.ui_in.value = 0x02
        dut.uio_in.value = 0
        dut.rst_n.value = 0
        await ClockCycles(dut.clk, 5)
        dut.rst_n.value = 1
        cocotb.start_soon(self._ticker())
        await ClockCycles(dut.clk, 3)

    async def _ticker(self):
        from cocotb.triggers import FallingEdge
        dut, b = self.dut, self.board
        while True:
            await FallingEdge(dut.clk)
            try:
                uo, uio_out, uio_oe = int(dut.uo_out.value), int(dut.uio_out.value), int(dut.uio_oe.value)
            except ValueError:
                uo, uio_out, uio_oe = 0, 0, 0
            b.resolve(uo, uio_out, uio_oe)
            keep = 0x3F if self.lm.quad else 0x07     # quad writes own GPIO8..10 (ui[5:3])
            self.lm.ui = (self.lm.ui & keep) | (b.ui_hi() & ~keep & 0xFF)
            dut.ui_in.value = self.lm.ui
            dut.uio_in.value = b.uio_in()
            b.tick_models()

    async def cycles(self, n):
        from cocotb.triggers import ClockCycles
        await ClockCycles(self.dut.clk, n)

    slow_host = True

    async def write(self, addr, *words, quad=False):
        self.lm.quad = quad
        try:
            await self.lm.write(addr, *words)
        finally:
            self.lm.quad = False

    async def read(self, addr, n=1):
        return await self.lm.read(addr, n)
