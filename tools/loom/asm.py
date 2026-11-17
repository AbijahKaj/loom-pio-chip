"""Loom assembler (PIO-flavoured syntax).

    .program uart_tx
    .side_set 1 opt            ; side-set bits [opt] [pindirs]
    .define BAUD_DIV 52
    .wrap_target
    start:
        pull block      side 1 [3]
        set x, 7
    bitloop:
        out pins, 1
        jmp x--, bitloop [2]
        out.c pins, 8           ; coded (line-helper) serial op
        wait 1 event pattern
        helper crc coder clear  ; SET HELPER
    .wrap

Comments start with ';', '#' or '//'. Numbers: 12, 0x1f, 0b101. `.define`
values and labels can be used in expressions.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import isa
from .regs import EVENT_NAMES, EngineConfig


class AsmError(Exception):
    pass


@dataclass
class Program:
    name: str
    code: List[int]
    labels: Dict[str, int]
    defines: Dict[str, int]
    wrap_target: int
    wrap: int
    sideset_count: int = 0
    sideset_opt: bool = False
    sideset_pindirs: bool = False
    source: List[str] = field(default_factory=list)

    def __len__(self):
        return len(self.code)

    def relocated(self, origin: int) -> List[int]:
        """Code with JMP targets offset by origin."""
        out = []
        for w in self.code:
            if (w >> 13) == isa.OP_JMP:
                w = (w & ~63) | ((w + origin) & 63)
            out.append(w)
        return out

    def addr(self, label: str, origin: int = 0) -> int:
        return (self.labels[label] + origin) & 63

    def config(self, origin: int = 0, **kw) -> EngineConfig:
        """An EngineConfig with wrap/side-set filled in from the program; kw overrides."""
        cfg = EngineConfig(
            wrap_bottom=(self.wrap_target + origin) & 63,
            wrap_top=(self.wrap + origin) & 63,
            start=origin & 63,
            sideset_count=self.sideset_count,
            sideset_opt=self.sideset_opt,
            sideset_pindirs=self.sideset_pindirs,
        )
        for k, v in kw.items():
            if not hasattr(cfg, k):
                raise AttributeError(k)
            setattr(cfg, k, v)
        return cfg

    def listing(self) -> str:
        lines = []
        for i, w in enumerate(self.code):
            lab = [k for k, v in self.labels.items() if v == i]
            lines.append(f"{i:2d}: {w:04x}  {isa.disasm(w, self.sideset_count, self.sideset_opt):32s}"
                         f"{'; ' + ','.join(lab) if lab else ''}")
        return "\n".join(lines)


_num_re = re.compile(r"^(0x[0-9a-fA-F_]+|0b[01_]+|\d+)$")


class _Ctx:
    def __init__(self, defines, labels):
        self.defines = defines
        self.labels = labels

    def val(self, expr: str, lineno: int) -> int:
        expr = expr.strip()
        names = dict(self.defines)
        names.update(self.labels)
        try:
            tokens = re.split(r"([A-Za-z_][A-Za-z0-9_]*)", expr)
            py = "".join(str(names[t]) if t in names else t for t in tokens)
            if re.search(r"[A-Za-z_]", re.sub(r"0x[0-9a-fA-F]+|0b[01]+", "", py)):
                raise AsmError(f"line {lineno}: unknown symbol in '{expr}'")
            return int(eval(py, {"__builtins__": {}}, {}))
        except AsmError:
            raise
        except Exception as e:
            raise AsmError(f"line {lineno}: bad expression '{expr}': {e}")


def _strip_comment(line: str) -> str:
    for c in (";", "#", "//"):
        i = line.find(c)
        if i >= 0:
            line = line[:i]
    return line.strip()


def assemble(text: str, defines: Optional[Dict[str, int]] = None) -> Program:
    defines = dict(defines or {})
    external = set(defines)          # externally supplied defines override .define defaults
    lines = text.splitlines()
    name = "program"
    ss_count, ss_opt, ss_pindirs = 0, False, False
    wrap_target, wrap = None, None
    labels: Dict[str, int] = {}
    body = []  # (lineno, text)

    # ---- pass 1: directives, labels
    pc = 0
    for lineno, raw in enumerate(lines, 1):
        line = _strip_comment(raw)
        if not line:
            continue
        while True:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
            if not m:
                break
            if m.group(1) in labels:
                raise AsmError(f"line {lineno}: duplicate label {m.group(1)}")
            labels[m.group(1)] = pc
            line = m.group(2).strip()
        if not line:
            continue
        if line.startswith("."):
            parts = line.split()
            d = parts[0].lower()
            if d == ".program":
                name = parts[1]
            elif d == ".side_set":
                ss_count = int(parts[1], 0)
                ss_opt = "opt" in parts[2:]
                ss_pindirs = "pindirs" in parts[2:]
                if ss_opt:
                    ss_count += 1
                if ss_count > 4:
                    raise AsmError(f"line {lineno}: at most 4 side-set bits (incl. opt)")
            elif d == ".define":
                rest = parts[1:]
                if rest and rest[0].upper() == "PUBLIC":
                    rest = rest[1:]
                if rest[0] not in external:
                    defines[rest[0]] = _Ctx(defines, {}).val(" ".join(rest[1:]), lineno)
            elif d == ".wrap_target":
                wrap_target = pc
            elif d == ".wrap":
                wrap = pc - 1
            elif d == ".origin":
                pass
            else:
                raise AsmError(f"line {lineno}: unknown directive {d}")
            continue
        body.append((lineno, line))
        pc += 1

    if pc > 64:
        raise AsmError(f"program too long ({pc} > 64)")
    ctx = _Ctx(defines, labels)
    code = []
    src = []
    dly_bits = 4 - ss_count
    max_delay = (1 << dly_bits) - 1

    for lineno, line in body:
        # trailing [delay] and side N
        delay = 0
        m = re.search(r"\[([^\]]+)\]\s*$", line)
        if m:
            delay = ctx.val(m.group(1), lineno)
            line = line[: m.start()].strip()
        side = None
        m = re.search(r"\bside\s+(\S+)\s*$", line)
        if m:
            side = ctx.val(m.group(1), lineno)
            line = line[: m.start()].strip()
        if delay > max_delay:
            raise AsmError(f"line {lineno}: delay {delay} > max {max_delay} with {ss_count} side-set bits")
        ds = delay
        if side is not None:
            if ss_count == 0:
                raise AsmError(f"line {lineno}: side-set used but .side_set not declared")
            nbits = ss_count - (1 if ss_opt else 0)
            if side >= (1 << nbits):
                raise AsmError(f"line {lineno}: side value {side} too large")
            sv = side | ((1 << nbits) if ss_opt else 0)
            ds |= sv << dly_bits
        elif ss_count and not ss_opt:
            raise AsmError(f"line {lineno}: side-set is mandatory (declare 'opt' to make it optional)")
        op, arg = _encode(line, ctx, lineno)
        code.append(isa.encode(op, ds, arg))
        src.append(line)

    if wrap_target is None:
        wrap_target = 0
    if wrap is None:
        wrap = len(code) - 1
    return Program(name, code, labels, defines, wrap_target, wrap, ss_count, ss_opt, ss_pindirs, src)


def _args(rest: str) -> List[str]:
    return [a.strip() for a in re.split(r"[,\s]+", rest.strip()) if a.strip()]


def _encode(line: str, ctx: _Ctx, lineno: int):
    parts = line.split(None, 1)
    mnem = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    err = lambda msg: AsmError(f"line {lineno}: {msg}: '{line}'")

    if mnem == "nop":
        return isa.OP_MOV, isa.mov(isa.MOV_DST["y"], 0, isa.MOV_SRC["y"])

    if mnem == "jmp":
        a = [x.strip() for x in rest.split(",")]
        if len(a) == 1:
            sub = a[0].split()
            if len(sub) == 2:
                a = sub
        if len(a) == 1:
            cond, target = "", a[0]
        elif len(a) == 2:
            cond, target = a[0].lower().replace(" ", ""), a[1]
        else:
            raise err("bad jmp")
        if cond not in isa.JMP_COND:
            raise err(f"bad condition {cond}")
        return isa.OP_JMP, isa.jmp(isa.JMP_COND[cond], ctx.val(target, lineno))

    if mnem == "wait":
        a = _args(rest)
        if len(a) < 3:
            raise err("wait needs: wait <pol> <gpio|pin|irq|event> <idx>")
        pol = ctx.val(a[0], lineno)
        src = a[1].lower()
        if src not in isa.WAIT_SRC:
            raise err(f"bad wait source {src}")
        if src == "event":
            idx = EVENT_NAMES[a[2].lower()] if a[2].lower() in EVENT_NAMES else ctx.val(a[2], lineno)
        else:
            idx = ctx.val(a[2], lineno)
        return isa.OP_WAIT, isa.wait(pol, isa.WAIT_SRC[src], idx)

    if mnem in ("in", "in.c"):
        a = _args(rest)
        if len(a) != 2 or a[0].lower() not in isa.IN_SRC:
            raise err("bad in")
        coded = mnem == "in.c"
        n = 0 if (coded and a[1].lower() == "x") else ctx.val(a[1], lineno)
        if coded and not 0 <= n <= 31:
            raise err("coded bit count must be 1..31, or 0 / x for X-counted")
        if not coded and not 1 <= n <= 32:
            raise err("bit count must be 1..32")
        return isa.OP_IN, isa.in_(isa.IN_SRC[a[0].lower()], n & 31, coded)

    if mnem in ("out", "out.c"):
        a = _args(rest)
        if len(a) != 2 or a[0].lower() not in isa.OUT_DST:
            raise err("bad out")
        coded = mnem == "out.c"
        n = 0 if (coded and a[1].lower() == "x") else ctx.val(a[1], lineno)
        if coded and not 0 <= n <= 31:
            raise err("coded bit count must be 1..31, or 0 / x for X-counted")
        if not coded and not 1 <= n <= 32:
            raise err("bit count must be 1..32")
        return isa.OP_OUT, isa.out(isa.OUT_DST[a[0].lower()], n & 31, coded)

    if mnem in ("push", "pull"):
        a = [x.lower() for x in _args(rest)]
        cond = ("iffull" in a) if mnem == "push" else ("ifempty" in a)
        block = "noblock" not in a
        return isa.OP_PUSHPULL, (isa.push(cond, block) if mnem == "push" else isa.pull(cond, block))

    if mnem == "mov":
        a = [x.strip() for x in rest.split(",")]
        if len(a) != 2:
            raise err("bad mov")
        dst = a[0].lower()
        s = a[1].replace(" ", "").lower()
        op = 0
        for pfx in ("::", "++", "~", "!"):
            if s.startswith(pfx):
                op = isa.MOV_OP[pfx]
                s = s[len(pfx):]
                break
        if dst not in isa.MOV_DST or s not in isa.MOV_SRC:
            raise err("bad mov operands")
        return isa.OP_MOV, isa.mov(isa.MOV_DST[dst], op, isa.MOV_SRC[s])

    if mnem == "irq":
        a = [x.lower() for x in _args(rest)]
        clear = "clear" in a
        wait = "wait" in a
        rel = "rel" in a
        nums = [x for x in a if x not in ("set", "nowait", "wait", "clear", "rel")]
        if len(nums) != 1:
            raise err("bad irq")
        return isa.OP_IRQ, isa.irq(ctx.val(nums[0], lineno), clear, wait, rel)

    if mnem == "set":
        a = [x.strip() for x in rest.split(",")]
        if len(a) != 2 or a[0].lower() not in isa.SET_DST:
            raise err("bad set")
        v = ctx.val(a[1], lineno)
        if not 0 <= v < 64:
            raise err("set immediate must be 0..63")
        return isa.OP_SET, isa.set_(isa.SET_DST[a[0].lower()], v)

    if mnem == "helper":
        v = 0
        for f in _args(rest):
            if f.lower() not in isa.HELPER_FLAGS:
                raise err(f"unknown helper flag {f}")
            v |= isa.HELPER_FLAGS[f.lower()]
        return isa.OP_SET, isa.set_(isa.SET_DST["helper"], v)

    raise err("unknown mnemonic")


def assemble_file(path: str, defines=None) -> Program:
    with open(path) as f:
        return assemble(f.read(), defines)
