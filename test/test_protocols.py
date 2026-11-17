# SPDX-License-Identifier: Apache-2.0
"""Protocol scenarios (scenarios.py) on the RTL, through the real SPI host interface."""
import os

import cocotb

import scenarios
from board import Board, CocotbBackend

ONLY = [s for s in os.environ.get("SCENARIOS", "").split(",") if s]


def make(name, fn, kw):
    async def run(dut):
        be = CocotbBackend(dut)
        b = Board(be)
        await be.start()
        res = await fn(b, **kw)
        dut._log.info(f"{name}: {res}")

    run.__name__ = f"test_{name}"
    run.__qualname__ = run.__name__
    return cocotb.test(skip=bool(ONLY) and name not in ONLY)(run)


for _name, _fn, _kw in scenarios.ALL:
    globals()[f"test_{_name}"] = make(_name, _fn, _kw)
