#!/usr/bin/env python3
"""Run the protocol scenarios on the Python ISS (fast).  usage: run_iss.py [name ...]"""
import asyncio
import sys
import time
import traceback

from board import Board, IssBackend
import scenarios


def main(names):
    ok = True
    for name, fn, kw in scenarios.ALL:
        if names and name not in names:
            continue
        b = Board(IssBackend())
        t0 = time.time()
        try:
            res = asyncio.run(fn(b, **kw))
            print(f"PASS {name:16s} {b.cycle:8d} cycles {time.time() - t0:6.1f}s  {res}")
        except Exception:
            ok = False
            print(f"FAIL {name:16s} {b.cycle:8d} cycles")
            traceback.print_exc()
    return ok


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1:]) else 1)
