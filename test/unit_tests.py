#!/usr/bin/env python3
"""Toolchain unit tests (no simulator needed beyond the Python ISS).  usage: python3 test/unit_tests.py"""
import glob
import os
import random
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
from loom import isa, regs as R  # noqa: E402
from loom.asm import AsmError, assemble, assemble_file  # noqa: E402
from loom.sim import Chip  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    if not cond:
        failures += 1
        print(f"FAIL {name} {detail}")
    else:
        print(f"PASS {name}")


# ---------------------------------------------------------------- assembler round trip
for f in sorted(glob.glob(os.path.join(ROOT, "programs", "*.loom"))):
    p = assemble_file(f)
    text = "\n".join([".program rt"] + ([f".side_set {p.sideset_count - p.sideset_opt}"
                                          + (" opt" if p.sideset_opt else "")
                                          + (" pindirs" if p.sideset_pindirs else "")] if p.sideset_count else [])
                     + [isa.disasm(w, p.sideset_count, p.sideset_opt) for w in p.code])
    p2 = assemble(text)
    check(f"roundtrip {p.name}", p2.code == p.code, f"{[hex(x) for x in p.code]} vs {[hex(x) for x in p2.code]}")

# ---------------------------------------------------------------- assembler errors
for bad in ["out pins, 0", "set x, 64", "jmp maybe, 0", "bogus", "out.c pins, 32",
            ".side_set 1\nnop", "nop [16]", ".side_set 2 opt\nnop [2]", "wait 1 event nosuch"]:
    try:
        assemble(bad)
        check(f"rejects '{bad}'", False)
    except (AsmError, KeyError):
        check(f"rejects '{bad.splitlines()[-1]}'", True)

# ---------------------------------------------------------------- config encoding <-> ISS decoding
rng = random.Random(7)
for t in range(50):
    cfg = R.EngineConfig(
        clkdiv=rng.choice([1, 2.5, 130.208, 65535]), wrap_bottom=rng.randrange(64), wrap_top=rng.randrange(64),
        start=rng.randrange(64), sideset_count=rng.randrange(5), sideset_opt=rng.random() < .5,
        jmp_pin=rng.randrange(20), in_shift_right=rng.random() < .5, autopull=rng.random() < .5,
        push_thresh=rng.randrange(32), pull_thresh=rng.randrange(32), out_base=rng.randrange(20),
        out_count=rng.randrange(21), set_base=rng.randrange(20), set_count=rng.randrange(6),
        sideset_base=rng.randrange(20), in_base=rng.randrange(20), code_mode=rng.randrange(3),
        stuff_en=rng.random() < .5, stuff_mode=rng.randrange(2), stuff_n=rng.randrange(16),
        diff=rng.random() < .5, idle_level=rng.randrange(2), ts_pin=rng.randrange(20), ts_edge=rng.randrange(4),
        dpll=rng.random() < .5, crc_width=rng.randrange(1, 33), crc_init_ones=rng.random() < .5,
        crc_poly=rng.getrandbits(32), crc_skip=rng.randrange(64), pat_mask=rng.getrandbits(20),
        pat_val=rng.getrandbits(20), trap_en=rng.random() < .5, trap_event=rng.randrange(8),
        trap_pol=rng.randrange(2), trap_addr=rng.randrange(64), timeout=rng.getrandbits(16))
    chip = Chip()
    chip.configure(0, cfg)
    c = chip.eng[0].c
    ok = (c["wrap_bot"], c["wrap_top"], c["start_addr"], c["ss_count"], c["ss_en"], c["jmp_pin"], c["out_base"],
          c["out_count"], c["set_count"], c["in_base"], c["c_mode"], c["c_stuff_n"], c["crc_width"],
          c["crc_skip_cfg"], c["crc_poly"], c["trap_addr"], c["timeout"], c["pat_val"]) == \
         (cfg.wrap_bottom, cfg.wrap_top, cfg.start, cfg.sideset_count, int(cfg.sideset_opt), cfg.jmp_pin,
          cfg.out_base, cfg.out_count, cfg.set_count, cfg.in_base, cfg.code_mode, cfg.stuff_n, cfg.crc_width,
          cfg.crc_skip, cfg.crc_poly, cfg.trap_addr, cfg.timeout, cfg.pat_val)
    if not ok:
        check(f"config encode/decode #{t}", False)
        break
else:
    check("config encode/decode (50 random configs)", True)


# ---------------------------------------------------------------- CRC helper vs. catalogue CRCs
def ref_crc(data, width, poly, init, refin, refout, xorout):
    """Plain bitwise reference implementation (Rocksoft model)."""
    crc = init
    top = 1 << (width - 1)
    mask = (1 << width) - 1
    for byte in data:
        if refin:
            byte = int(f"{byte:08b}"[::-1], 2)
        for i in range(7, -1, -1):
            bit = (byte >> i) & 1
            fb = bit ^ (1 if crc & top else 0)
            crc = (crc << 1) & mask
            if fb:
                crc ^= poly
    if refout:
        crc = int(f"{crc:0{width}b}"[::-1], 2)
    return crc ^ xorout


def loom_crc(data, width, poly, init_ones, refin):
    """Feed bytes through the engine CRC with `out.c null, 8` and read the register back."""
    p = assemble("""
        pull block
        out.c null, 8
        """)
    chip = Chip()
    chip.load(p, 0)
    cfg = p.config(0, crc_width=width, crc_poly=poly, crc_init_ones=init_ones, out_shift_right=refin)
    chip.configure(0, cfg)
    chip.write(R.CTRL, 0x11)
    for b in data:
        chip.write(R.ENG(0, R.TXF), b if refin else b << 24)
        for _ in range(20):
            chip.step()
    return chip.eng[0].s["crc"]


CATALOGUE = [  # name, width, poly, init, refin, refout, xorout, check("123456789")
    ("CRC-8/SMBUS", 8, 0x07, 0x00, False, False, 0x00, 0xF4),
    ("CRC-16/XMODEM", 16, 0x1021, 0x0000, False, False, 0x0000, 0x31C3),
    ("CRC-16/USB", 16, 0x8005, 0xFFFF, True, True, 0xFFFF, 0xB4C8),
    ("CRC-15/CAN", 15, 0x4599, 0x0000, False, False, 0x0000, 0x059E),
    ("CRC-32/ISO-HDLC", 32, 0x04C11DB7, 0xFFFFFFFF, True, True, 0xFFFFFFFF, 0xCBF43926),
    ("CRC-32/MPEG-2", 32, 0x04C11DB7, 0xFFFFFFFF, False, False, 0x00000000, 0x0376E6E7),
    ("CRC-5/USB", 5, 0x05, 0x1F, True, True, 0x1F, 0x19),
]
msg = b"123456789"
for name, w, poly, init, refin, refout, xorout, chk in CATALOGUE:
    ref = ref_crc(msg, w, poly, init, refin, refout, xorout)
    reg = loom_crc(msg, w, poly, init != 0, refin)
    # the engine register holds the raw (non-reflected, non-inverted) remainder;
    # software applies refout / xorout with `mov ::` and `mov ~` as needed
    got = int(f"{reg:0{w}b}"[::-1], 2) if refout else reg
    got ^= xorout
    check(f"CRC {name}", ref == chk and got == chk, f"ref={ref:#x} loom={got:#x} check={chk:#x}")

print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURES'}")
sys.exit(1 if failures else 0)
