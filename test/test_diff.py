# SPDX-License-Identifier: Apache-2.0
"""Differential (lockstep) test: RTL vs. the Python ISS (tools/loom/sim.py).

Random programs + random engine configuration + random pin stimulus + random
host traffic. The host bus strobes are taken from the RTL's SPI block and fed
to the ISS, then every architectural register of every engine, the GPIO
output/OE registers and the IRQ flags are compared after every clock edge.
"""
import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

import loomtb
from loomtb import Loom, R
from loom.sim import Chip

SEEDS = int(os.environ.get("DIFF_SEEDS", "6"))
RUN_CYCLES = int(os.environ.get("DIFF_CYCLES", "3000"))

TOTAL_COV = {}
REQUIRED_COV = ["op0", "op1", "op1_stall", "op2", "op2_stall", "op3", "op3_stall", "op4", "op4_stall", "op5",
                "op6", "op6_stall", "op7", "trap_fire", "coded_in_mode0", "coded_in_mode1", "coded_in_mode2",
                "coded_out_mode0", "coded_out_mode1", "coded_out_mode2", "coded_repeat", "stuff_insert",
                "stuff_err", "rx_push", "tx_pop", "crc_step", "sideset", "jump", "ts_edge"]

ENGINE_SIGS = ["pc", "x", "y", "osr", "isr", "osr_cnt", "isr_cnt", "delay_cnt", "irq_wait", "cb_active",
               "cb_left", "div_cnt", "div_acc", "stuff_on", "run", "last_bit", "tx_lvl", "rx_lvl", "mph",
               "stuff_err", "crc", "crc_skip", "last_ts", "delta_ts", "edge_flag", "trap_armed"]


class Lockstep:
    def __init__(self, dut, chip, lm):
        self.lm = lm
        self.dut = dut
        self.top = dut.user_project
        self.chip = chip
        self.engs = [getattr(self.top, "eng")[i].u_eng for i in range(4)]
        self.compare = False
        self.cycles = 0
        self.mismatch = None
        self.pin_rand = None
        self.hi = 0
        self.uio = 0

    def check(self):
        top, chip = self.top, self.chip
        errs = []
        if int(top.gpio_out.value) != chip.gpio_out:
            errs.append(f"gpio_out rtl={int(top.gpio_out.value):05x} iss={chip.gpio_out:05x}")
        if int(top.gpio_oe.value) != chip.gpio_oe:
            errs.append(f"gpio_oe rtl={int(top.gpio_oe.value):02x} iss={chip.gpio_oe:02x}")
        if int(top.irq.value) != chip.irq:
            errs.append(f"irq rtl={int(top.irq.value):02x} iss={chip.irq:02x}")
        for i, e in enumerate(self.engs):
            s = chip.eng[i].s
            for sig in ENGINE_SIGS:
                rv = int(getattr(e, sig).value)
                if rv != s[sig]:
                    errs.append(f"eng{i}.{sig} rtl={rv:#x} iss={s[sig]:#x}")
            if int(e.u_txf.level.value) != len(chip.eng[i].txf.q):
                errs.append(f"eng{i}.txf level rtl={int(e.u_txf.level.value)} iss={len(chip.eng[i].txf.q)}")
            if int(e.u_rxf.level.value) != len(chip.eng[i].rxf.q):
                errs.append(f"eng{i}.rxf level rtl={int(e.u_rxf.level.value)} iss={len(chip.eng[i].rxf.q)}")
        return errs

    async def run(self):
        dut, top = self.dut, self.top
        while True:
            if self.compare:
                errs = self.check()
                if errs and self.mismatch is None:
                    self.mismatch = (self.cycles, errs)
            if self.pin_rand is not None and self.compare:
                self.hi = self.pin_rand.getrandbits(5) << 3
                self.uio = self.pin_rand.getrandbits(8)
            ui = self.hi | (self.lm.ui & 7)
            uio = self.uio
            dut.ui_in.value = ui
            dut.uio_in.value = uio
            wr = (int(top.h_wr_addr.value), int(top.h_wdata.value)) if int(top.h_wr.value) else None
            rd_pop = int(top.h_addr.value) if int(top.h_rd_pop.value) else None
            self.chip.step(ui, uio, wr, rd_pop)
            self.cycles += 1
            await FallingEdge(dut.clk)


def rand_program(rng, n):
    words = []
    for _ in range(n):
        op = rng.choice([0, 0, 1, 2, 2, 3, 3, 3, 4, 5, 5, 6, 7, 7, 2, 3])
        ds = rng.choice([0, 0, 0, rng.getrandbits(4)])
        arg = rng.getrandbits(9)
        if op == 0:
            arg = (arg & ~63) | rng.randrange(n)          # stay inside the program
        if op in (2, 3) and rng.random() < 0.5:
            arg = (arg & 0x1F) | (1 << 5) | (rng.choice([0, 0, 3]) << 6)   # coded serial ops
            if rng.random() < 0.3:
                arg &= ~0x1F                              # X-counted
        if op == 4 and rng.random() < 0.6:
            arg &= ~(1 << 6)                              # non-blocking push/pull
        if op == 1:
            # avoid waiting forever on IRQs nobody sets: mostly pins/events
            if (arg >> 6) & 3 == 2 and rng.random() < 0.7:
                arg = (arg & ~(3 << 6)) | (3 << 6)
        if op == 5 and (arg >> 6) == 5:
            arg &= ~(7 << 6)                              # mov pc is fine, but keep jumps rarer
        if op == 3 and (arg >> 6) == 5 and rng.random() < 0.7:
            arg &= ~(7 << 6)
        words.append((op << 13) | (ds << 9) | arg)
    return words


def rand_config(rng, n_prog):
    c = R.EngineConfig()
    c.clkdiv = rng.choice([1, 1, 1, 1.5, 2, 3, 2.25])
    c.wrap_bottom = rng.randrange(n_prog)
    c.wrap_top = rng.randrange(c.wrap_bottom, n_prog)
    c.start = rng.randrange(n_prog)
    c.sideset_count = rng.choice([0, 0, 1, 2, 3])
    c.sideset_opt = rng.random() < 0.4 and c.sideset_count > 0
    c.sideset_pindirs = rng.random() < 0.2
    c.jmp_pin = rng.randrange(20)
    c.in_shift_right = rng.random() < 0.5
    c.out_shift_right = rng.random() < 0.5
    c.autopush = rng.random() < 0.5
    c.autopull = rng.random() < 0.5
    c.push_thresh = rng.randrange(32)
    c.pull_thresh = rng.randrange(32)
    c.out_base, c.set_base, c.sideset_base, c.in_base = (rng.randrange(20) for _ in range(4))
    c.out_count = rng.randrange(0, 21)
    c.set_count = rng.randrange(0, 6)
    c.code_mode = rng.randrange(4)
    c.nrzi_toggle_on = rng.getrandbits(1)
    c.manch_pol = rng.getrandbits(1)
    c.stuff_en = rng.random() < 0.8
    c.stuff_mode = rng.getrandbits(1)
    c.stuff_n = rng.choice([1, 2, 2, 3, 4, 6, rng.randrange(16)])
    c.diff = rng.random() < 0.3
    c.idle_level = rng.getrandbits(1)
    c.ts_pin = rng.randrange(20)
    c.ts_edge = rng.randrange(4)
    c.dpll = rng.random() < 0.3
    c.crc_width = rng.randrange(0, 34)
    c.crc_init_ones = rng.random() < 0.5
    c.crc_skip = rng.choice([0, 0, 1, 3, 8])
    c.crc_poly = rng.getrandbits(32)
    c.pat_mask = rng.getrandbits(20)
    c.pat_val = rng.getrandbits(20)
    c.trap_en = rng.random() < 0.3
    c.trap_event = rng.randrange(8)
    c.trap_pol = rng.getrandbits(1)
    c.trap_addr = rng.randrange(n_prog)
    c.timeout = rng.getrandbits(8)
    return c


@cocotb.test()
async def test_diff_random(dut):
    loomtb.SPI_HALF = 4
    for seed in range(SEEDS):
        rng = random.Random(1000 + seed)
        dut._log.info(f"seed {seed}")
        lm = Loom(dut)
        if seed == 0:
            cocotb.start_soon(Clock(dut.clk, loomtb.CLK_NS, unit="ns").start())
        dut.ena.value = 1
        dut.ui_in.value = 0x02
        dut.uio_in.value = 0
        lm.ui = 0x02
        await FallingEdge(dut.clk)
        dut.rst_n.value = 0
        await ClockCycles(dut.clk, 3)
        await FallingEdge(dut.clk)
        dut.rst_n.value = 1
        chip = Chip()
        ls = Lockstep(dut, chip, lm)
        task = cocotb.start_soon(ls.run())

        n_prog = rng.randrange(4, 33)
        words = rand_program(rng, n_prog) + rand_program(rng, 32)[n_prog:]   # fill all 32 imem words
        await lm.write(R.IMEM_ADDR, 0)
        await lm.write(R.IMEM_DATA, *words)
        cfgs = [rand_config(rng, n_prog) for _ in range(4)]
        for i, c in enumerate(cfgs):
            regs = c.regs()
            for r in sorted(regs):
                await lm.write(R.ENG(i, r), regs[r])
            await lm.write(R.ENG(i, R.CRCVAL), rng.getrandbits(32))
        await lm.write(R.ENG(0, R.TXF), rng.getrandbits(32), rng.getrandbits(32))
        await lm.write(R.ENG(2, R.TXF), rng.getrandbits(32))
        ls.compare = True
        ls.pin_rand = random.Random(seed)
        en_mask = rng.choice([0b0001, 0b0011, 0b1111, 0b1010])
        await lm.write(R.CTRL, en_mask | (en_mask << 4))

        # random host traffic while running
        t_end = ls.cycles + RUN_CYCLES
        while ls.cycles < t_end and ls.mismatch is None:
            act = rng.random()
            n = rng.randrange(4)
            if act < 0.4:
                await lm.write(R.ENG(n, R.TXF), *[rng.getrandbits(32) for _ in range(rng.randrange(1, 3))])
            elif act < 0.6:
                await lm.read(R.ENG(n, R.RXF), rng.randrange(1, 3))
            elif act < 0.7:
                await lm.write(R.IRQ, rng.getrandbits(16))
            elif act < 0.75:
                await lm.write(R.CTRL, en_mask | (rng.getrandbits(4) << 4) | (rng.getrandbits(4) << 8)
                               | (rng.getrandbits(4) << 12))
            elif act < 0.8:
                await lm.write(R.GPIO, rng.getrandbits(20))
            elif act < 0.83:
                await lm.write(R.ENG(n, R.CODECFG), cfgs[n].regs()[R.CODECFG] ^ rng.getrandbits(4))
            else:
                await ClockCycles(dut.clk, rng.randrange(20, 200))
        task.cancel()
        if ls.mismatch:
            cyc, errs = ls.mismatch
            for e in errs[:20]:
                dut._log.error(e)
            for i, e in enumerate(ls.engs):
                dut._log.error(f"eng{i} pc={int(e.pc.value)} instr={int(e.instr.value):04x}")
            assert False, f"seed {seed}: RTL/ISS mismatch at cycle {cyc}"
        dut._log.info(f"seed {seed}: {ls.cycles} cycles in lockstep, OK")
        for i, e in enumerate(chip.eng):
            for k, v in e.cov.items():
                TOTAL_COV[k] = TOTAL_COV.get(k, 0) + v
    dut._log.info("coverage: " + ", ".join(f"{k}={v}" for k, v in sorted(TOTAL_COV.items())))
    missing = [k for k in REQUIRED_COV if TOTAL_COV.get(k, 0) == 0]
    assert not missing, f"coverage holes: {missing}"
