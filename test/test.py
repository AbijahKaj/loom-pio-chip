# SPDX-FileCopyrightText: © 2026 AbijahKaj
# SPDX-License-Identifier: Apache-2.0
"""Smoke test (also used for gate-level simulation): host SPI + one UART TX engine."""
import cocotb
from cocotb.triggers import ClockCycles, FallingEdge

from loomtb import Loom, R, prog


async def uart_decode(lm, gpio, bit_cycles, nbytes, timeout=200000):
    """Independent UART receiver model: samples the pad every clock."""
    out = []
    cyc = 0
    while len(out) < nbytes and cyc < timeout:
        await FallingEdge(lm.dut.clk)
        cyc += 1
        if lm.gpio(gpio) == 0:
            await ClockCycles(lm.dut.clk, bit_cycles // 2)
            assert lm.gpio(gpio) == 0, "glitch on start bit"
            b = 0
            for i in range(8):
                await ClockCycles(lm.dut.clk, bit_cycles)
                b |= lm.gpio(gpio) << i
            await ClockCycles(lm.dut.clk, bit_cycles)
            assert lm.gpio(gpio) == 1, "framing error (stop bit)"
            out.append(b)
    return out


@cocotb.test()
async def test_smoke(dut):
    lm = Loom(dut)
    await lm.start()

    assert await lm.read(R.ID) == R.LOOM_ID, "bad ID"
    await lm.write(R.OD, 0xA5)
    assert await lm.read(R.OD) == 0xA5
    await lm.write(R.OD, 0)

    # UART TX on GPIO13 (uo[1]): 4 ticks/bit, clkdiv 4 -> 16 clocks per bit
    p = prog("uart_tx")
    await lm.load(p, 0)
    await lm.configure(0, p.config(0, clkdiv=4, out_base=13, out_count=1, sideset_base=13,
                                   out_shift_right=True))
    await lm.push(0, 0x55, 0xA3)
    await lm.enable(0b0001)
    got = await uart_decode(lm, 13, 16, 3)
    if len(got) < 3:
        await lm.push(0, 0x0F)
        got += await uart_decode(lm, 13, 16, 3 - len(got))
    assert got[:2] == [0x55, 0xA3], got
    dut._log.info(f"UART TX bytes: {[hex(b) for b in got]}")
