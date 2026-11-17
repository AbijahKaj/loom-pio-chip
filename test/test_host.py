# SPDX-License-Identifier: Apache-2.0
"""Runs the *actual* MicroPython host driver (host/loomhost.py) against the RTL.

The driver is ordinary blocking code that toggles pins. cocotb's bridge/resume
lets each pin write advance simulated time, so the driver's real bit-banged SPI
waveforms (single and quad) drive the chip. The app it loads is produced by
tools/loom/export.py, exactly as on the board.
"""
import os
import subprocess
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.task import bridge, resume
from cocotb.triggers import ClockCycles, FallingEdge

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "host"))
import loomhost  # noqa: E402
import models as M  # noqa: E402

HALF = 5   # clocks per pin change (SCK = clk / 10)


class DutPins:
    def __init__(self, dut):
        self.dut = dut
        self.ui = 0x02

    @resume
    async def _settle(self):
        await ClockCycles(self.dut.clk, HALF)

    def pin(self, bit):
        pins = self

        class P:
            def value(self, v=None):
                if v is None:
                    return int(pins.dut.uo_out.value) & 1      # input pin: MISO (uo[0])
                pins.ui = (pins.ui & ~(1 << bit)) | ((v & 1) << bit)
                pins.dut.ui_in.value = pins.ui
                pins._settle()

        return P()


@cocotb.test()
async def test_host_driver(dut):
    cocotb.start_soon(Clock(dut.clk, 16, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0x02
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 3)

    # export the app exactly as a user would (UART TX on GPIO13, 1 Mbaud at this clock)
    out = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools/loom/export.py"),
         "0:0:" + os.path.join(ROOT, "programs/uart_tx.loom")
         + ":clkdiv=15,out_base=13,out_count=1,sideset_base=13,out_shift_right=1", "--gpio", "0x2000"],
        capture_output=True, text=True, check=True).stdout
    ns = {}
    exec(out, ns)
    app = ns["APP"]

    mon = M.UartMonitor(13, 60)
    lv = [0] * 20

    async def watch():
        while True:
            await FallingEdge(dut.clk)
            lv[13] = (int(dut.uo_out.value) >> 1) & 1
            mon.tick(0, lv)

    cocotb.start_soon(watch())
    pins = DutPins(dut)

    @bridge
    def host():
        h = loomhost.Loom(sck=pins.pin(0), cs_n=pins.pin(1), mosi=pins.pin(2), miso=pins.pin(0),
                          io=(pins.pin(3), pins.pin(4), pins.pin(5)))
        h.check()
        h.run(app)
        h.push(0, 0x48, 0x69)                 # single-bit SPI
        h.set_quad(True)
        h.push(0, 0x21, 0x0A)                 # quad-write SPI
        h.set_quad(False)                     # (this write goes out in quad mode, as required)
        return h.read(loomhost.FLEVEL)

    await host()
    for _ in range(4 * 10 * 60 + 200):
        await FallingEdge(dut.clk)
        if len(mon.bytes) >= 4:
            break
    await ClockCycles(dut.clk, 700)
    assert bytes(mon.bytes) == b"Hi!\n", mon.bytes
    assert mon.errors == 0
