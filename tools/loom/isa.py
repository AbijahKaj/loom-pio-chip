"""Loom ISA definitions (16-bit instructions).

    15  13 12     9 8                     0
   +-----+--------+------------------------+
   | op  | dly/ss |        operands        |
   +-----+--------+------------------------+
"""

OP_JMP, OP_WAIT, OP_IN, OP_OUT, OP_PUSHPULL, OP_MOV, OP_IRQ, OP_SET = range(8)
OP_NAMES = ["jmp", "wait", "in", "out", "pushpull", "mov", "irq", "set"]

JMP_COND = {"": 0, "always": 0, "!x": 1, "x--": 2, "!y": 3, "y--": 4, "x!=y": 5, "pin": 6, "!osre": 7}
WAIT_SRC = {"gpio": 0, "pin": 1, "irq": 2, "event": 3}
IN_SRC = {"pins": 0, "x": 1, "y": 2, "null": 3, "tstamp": 4, "crc": 5, "isr": 6, "osr": 7}
OUT_DST = {"pins": 0, "x": 1, "y": 2, "null": 3, "pindirs": 4, "pc": 5, "isr": 6}
MOV_DST = {"pins": 0, "x": 1, "y": 2, "crc": 3, "pindirs": 4, "pc": 5, "isr": 6, "osr": 7}
MOV_SRC = {"pins": 0, "x": 1, "y": 2, "null": 3, "status": 4, "crc": 5, "isr": 6, "osr": 7}
MOV_OP = {"": 0, "~": 1, "!": 1, "::": 2, "++": 3}
SET_DST = {"pins": 0, "x": 1, "y": 2, "helper": 3, "pindirs": 4}

# SET HELPER flag bits
HELPER_FLAGS = {
    "crc": 1 << 0,        # reset CRC to its init value
    "coder": 1 << 1,      # reset coder state (stuff run, NRZI level, Manchester phase)
    "clear": 1 << 2,      # clear sticky flags (stuff error, edge seen)
    "arm": 1 << 3,        # re-arm the event trap
    "stuff_off": 1 << 4,  # disable bit-stuffing
    "stuff_on": 2 << 4,   # enable bit-stuffing
}


def encode(op: int, ds: int, arg: int) -> int:
    assert 0 <= op < 8 and 0 <= ds < 16 and 0 <= arg < 512
    return (op << 13) | (ds << 9) | arg


def jmp(cond: int, addr: int) -> int:
    return (cond << 6) | (addr & 63)


def wait(pol: int, src: int, idx: int) -> int:
    return (pol << 8) | (src << 6) | (idx & 63)


def in_(src: int, n: int, coded=False) -> int:
    return (src << 6) | (int(coded) << 5) | (n & 31)


def out(dst: int, n: int, coded=False) -> int:
    return (dst << 6) | (int(coded) << 5) | (n & 31)


def push(iffull=False, block=True) -> int:
    return (0 << 8) | (int(iffull) << 7) | (int(block) << 6)


def pull(ifempty=False, block=True) -> int:
    return (1 << 8) | (int(ifempty) << 7) | (int(block) << 6)


def mov(dst: int, op: int, src: int) -> int:
    return (dst << 6) | (op << 4) | src


def irq(idx: int, clear=False, wait=False, rel=False) -> int:
    return (int(clear) << 8) | (int(wait) << 7) | (int(rel) << 4) | (idx & 7)


def set_(dst: int, imm: int) -> int:
    return (dst << 6) | (imm & 63)


def disasm(word: int, sideset_count=0, sideset_opt=False) -> str:
    op, ds, arg = word >> 13, (word >> 9) & 15, word & 511
    hi = arg >> 6
    inv = lambda d: {v: k for k, v in d.items() if k not in ("", "always", "!")}
    if op == OP_JMP:
        c = {v: k for k, v in JMP_COND.items() if k != "always"}[hi]
        s = f"jmp {c + ', ' if c else ''}{arg & 63}"
    elif op == OP_WAIT:
        s = f"wait {arg >> 8} {inv(WAIT_SRC)[(arg >> 6) & 3]} {arg & 63}"
    elif op == OP_IN:
        s = f"in{'.c' if arg & 32 else ''} {inv(IN_SRC)[hi]}, {arg & 31 or 32}"
    elif op == OP_OUT:
        s = f"out{'.c' if arg & 32 else ''} {inv(OUT_DST).get(hi, 'rsvd')}, {arg & 31 or 32}"
    elif op == OP_PUSHPULL:
        if arg >> 8:
            s = "pull" + (" ifempty" if arg & 128 else "") + (" block" if arg & 64 else " noblock")
        else:
            s = "push" + (" iffull" if arg & 128 else "") + (" block" if arg & 64 else " noblock")
    elif op == OP_MOV:
        o = {0: "", 1: "~", 2: "::", 3: "++"}[(arg >> 4) & 3]
        s = f"mov {inv(MOV_DST)[hi]}, {o}{inv(MOV_SRC)[arg & 7]}"
    elif op == OP_IRQ:
        kind = "clear" if arg & 256 else ("wait" if arg & 128 else "set")
        s = f"irq {kind} {arg & 7}{' rel' if arg & 16 else ''}"
    else:
        if hi == 3:
            s = "helper " + " ".join(k for k, v in HELPER_FLAGS.items() if (arg & 63) & v == v and v) or "helper"
        else:
            s = f"set {inv(SET_DST).get(hi, 'rsvd')}, {arg & 63}"
    dly_bits = 4 - sideset_count
    delay = ds & ((1 << dly_bits) - 1)
    side = ds >> dly_bits
    if sideset_count:
        if sideset_opt:
            if side >> (sideset_count - 1):
                s += f" side {side & ((1 << (sideset_count - 1)) - 1)}"
        else:
            s += f" side {side}"
    if delay:
        s += f" [{delay}]"
    return s
