"""Cycle-exact instruction-set simulator (golden model) for Loom.

Models the chip *behind* the host SPI interface: the host side is driven by
bus events (register writes / RX-FIFO pops) that take effect at the clock edge
of the cycle in which they are presented -- exactly like the `wr`/`rd_pop`
strobes inside the RTL. Pin inputs pass through the same 2-flop synchroniser.

Every `step()` models one rising clock edge. The structure deliberately
mirrors src/loom_engine.v (combinational next-state, then sequential
overrides) so that the two can be diffed state-by-state in
test/test_diff.py.
"""
from . import regs as R

M32 = 0xFFFFFFFF


def rotl(v, s):
    s &= 31
    return ((v << s) | (v >> (32 - s))) & M32 if s else v & M32


def rotr(v, s):
    s &= 31
    return ((v >> s) | (v << (32 - s))) & M32 if s else v & M32


def nmask(n):
    return (1 << n) - 1 if n < 32 else M32


def bitrev(v):
    return int(f"{v & M32:032b}"[::-1], 2)


def sat_add(a, b):
    return 32 if a + b > 32 else a + b


class Fifo:
    def __init__(self, depth=2):
        self.depth = depth
        self.q = []

    @property
    def empty(self):
        return not self.q

    @property
    def full(self):
        return len(self.q) >= self.depth

    @property
    def head(self):
        return self.q[0] if self.q else 0  # RTL returns stale memory; never consumed when empty

    def edge(self, clr, push, wdata, pop):
        if clr:
            self.q = []
            return
        do_push = push and not self.full
        do_pop = pop and not self.empty
        if do_pop:
            self.q.pop(0)
        if do_push:
            self.q.append(wdata & M32)


CFG_FIELDS = dict(
    div_int=1, div_frac=0, wrap_bot=0, wrap_top=63, start_addr=0, ss_count=0, ss_en=0, ss_pindir=0,
    jmp_pin=0, in_shr=0, out_shr=0, autopush=0, autopull=0, push_th=0, pull_th=0, out_base=0,
    out_count=0, set_base=0, set_count=0, side_base=0, in_base=0, c_mode=0, c_nrzi_tog=0,
    c_manch_pol=0, c_stuff_en=0, c_stuff_mode=0, c_stuff_n=0, c_diff=0, c_idle=0, ts_pin=0, ts_edge=0,
    dpll_en=0, crc_width=0, crc_skip_cfg=0, crc_init1=0, crc_poly=0, pat_mask=0, pat_val=0, trap_en=0, trap_pol=0,
    trap_ev=0, trap_addr=0, timeout=0,
)
STATE_FIELDS = dict(
    pc=0, x=0, y=0, osr=0, isr=0, osr_cnt=32, isr_cnt=0, delay_cnt=0, irq_wait=0, cb_active=0,
    cb_left=0, div_cnt=0, div_acc=0, stuff_on=0, run=0, last_bit=0, tx_lvl=0, rx_lvl=0, mph=0,
    stuff_err=0, crc=0, crc_skip=0, ts_prev=0, last_ts=0, delta_ts=0, edge_flag=0, trap_armed=0,
)


class Engine:
    def __init__(self, eid, fifo_depth=2):
        self.id = eid
        self.c = dict(CFG_FIELDS)
        self.s = dict(STATE_FIELDS)
        self.txf = Fifo(fifo_depth)
        self.rxf = Fifo(fifo_depth)
        self.trace = []
        self.cov = {}

    def _cov(self, k):
        self.cov[k] = self.cov.get(k, 0) + 1

    # ------------------------------------------------------------ combinational
    def comb(self, en, instr, gpio_in, irq_flags, ts):
        c, s = self.c, self.s
        nx = dict(s)
        out = dict(po_mask=0, po_val=0, pd_mask=0, pd_val=0, irq_set=0, irq_clr=0,
                   txf_pop=0, rxf_push=0, rxf_wdata=s["isr"], tick=0, exec=0)

        pins32 = gpio_in & 0xFFFFF
        pins_in = rotr(pins32, c["in_base"])
        push_th6 = 32 if c["push_th"] == 0 else c["push_th"]
        pull_th6 = 32 if c["pull_th"] == 0 else c["pull_th"]
        crc_w6 = 32 if (c["crc_width"] == 0 or c["crc_width"] > 32) else c["crc_width"]
        crc_wmask = nmask(crc_w6)
        crc_msb = (s["crc"] >> (crc_w6 - 1)) & 1

        tick = en and s["div_cnt"] == 0
        ts_pin_v = (pins32 >> c["ts_pin"]) & 1
        ts_edge_ev = ((ts_pin_v & (s["ts_prev"] ^ 1)) & (c["ts_edge"] & 1)) | \
                     (((ts_pin_v ^ 1) & s["ts_prev"]) & (c["ts_edge"] >> 1))
        since_edge = (ts - s["last_ts"]) & 0xFFFF

        txf, rxf = self.txf, self.rxf
        events = [
            int((pins32 & c["pat_mask"]) == (c["pat_val"] & c["pat_mask"])),
            s["stuff_err"],
            int(((s["crc"] ^ s["y"]) & crc_wmask) == 0),
            int(since_edge > c["timeout"]),
            s["edge_flag"],
            int(txf.empty),
            int(rxf.full),
            1,
        ]
        trap_fire = s["trap_armed"] and events[c["trap_ev"]] == c["trap_pol"]

        op, ds, arg = instr >> 13, (instr >> 9) & 15, instr & 511
        f_hi = arg >> 6
        coded = (arg >> 5) & 1
        dly_bits = (4 - c["ss_count"]) & 7
        delay = ds & (~(0xF << dly_bits) & 0xF)
        side_raw = (ds >> dly_bits) & 0xF
        side_n = (c["ss_count"] - c["ss_en"]) & 7
        side_v = side_raw & (~(0xF << side_n) & 0xF)
        side_act = c["ss_count"] != 0 and side_n != 0 and (not c["ss_en"] or (ds >> 3) & 1)
        n6 = 32 if (arg & 31) == 0 else (arg & 31)
        xmode = (arg & 31) == 0
        x_dec = (s["x"] - 1) & M32
        cb_cur = s["cb_left"] if s["cb_active"] else n6
        cnt_last = (s["x"] == 1) if xmode else (cb_cur == 1)
        cnt_zero = (s["x"] == 0) if xmode else (cb_cur == 0)
        stuff_now = bool(s["stuff_on"] and c["c_stuff_n"] != 0 and s["run"] == c["c_stuff_n"])

        exec_ = tick and not trap_fire and s["delay_cnt"] == 0
        out["tick"], out["exec"] = int(bool(tick)), int(bool(exec_))

        apull_need = c["autopull"] and s["osr_cnt"] >= pull_th6
        osr_e = txf.head if (apull_need and not txf.empty) else s["osr"]
        osr_cnt_e = 0 if (apull_need and not txf.empty) else s["osr_cnt"]

        src_sel = (arg & 7) if op == 5 else f_hi
        if src_sel == 0:
            srcv = pins_in
        elif src_sel == 1:
            srcv = s["x"]
        elif src_sel == 2:
            srcv = s["y"]
        elif src_sel == 3:
            srcv = 0
        elif src_sel == 4:
            if op == 5:
                srcv = (since_edge << 16) | (s["edge_flag"] << 13) | (s["stuff_err"] << 12) \
                    | (s["osr_cnt"] << 6) | s["isr_cnt"]
            else:
                srcv = s["delta_ts"]
        elif src_sel == 5:
            srcv = s["crc"]
        elif src_sel == 6:
            srcv = s["isr"]
        else:
            srcv = s["osr"]

        is_in = op == 2
        f_hi_in = srcv if is_in else 0
        f_lo_in = s["isr"] if is_in else osr_e
        f_amt = (32 - n6) if (not is_in and not c["out_shr"]) else n6
        funnel = (((f_hi_in << 32) | f_lo_in) >> f_amt) & M32
        l_in = s["isr"] if is_in else osr_e
        lshift = (l_in << n6) & M32
        nmask_n = nmask(n6)
        in_new = funnel if c["in_shr"] else (lshift | (srcv & nmask_n))
        out_data = (osr_e & nmask_n) if c["out_shr"] else funnel
        out_osr = funnel if c["out_shr"] else lshift

        nx["edge_flag"] = s["edge_flag"] | ts_edge_ev
        pw_en, pw_dir, pw_base, pw_cnt, pw_val = 0, 0, c["out_base"], c["out_count"], 0
        stall = jump = rpt = 0
        target = 0
        crc_step = crc_bit = 0
        need_stuff = 0
        stuff_bit = 0
        run = s["run"]
        run_inc = run if run == 15 else run + 1

        if tick and trap_fire:
            nx.update(pc=c["trap_addr"], trap_armed=0, delay_cnt=0, cb_active=0, irq_wait=0, mph=0)
        elif tick and s["delay_cnt"] != 0:
            nx["delay_cnt"] = s["delay_cnt"] - 1
        elif exec_:
            x, y = s["x"], s["y"]
            if op == 0:  # JMP
                if f_hi == 0:
                    cond = 1
                elif f_hi == 1:
                    cond = x == 0
                elif f_hi == 2:
                    cond = x != 0
                    nx["x"] = (x - 1) & M32
                elif f_hi == 3:
                    cond = y == 0
                elif f_hi == 4:
                    cond = y != 0
                    nx["y"] = (y - 1) & M32
                elif f_hi == 5:
                    cond = x != y
                elif f_hi == 6:
                    cond = (pins32 >> c["jmp_pin"]) & 1
                else:
                    cond = s["osr_cnt"] < pull_th6
                if cond:
                    jump, target = 1, arg & 63
            elif op == 1:  # WAIT
                src = (arg >> 6) & 3
                if src == 0:
                    wv = (pins32 >> (arg & 31)) & 1
                elif src == 1:
                    wv = (pins_in >> (arg & 31)) & 1
                elif src == 2:
                    wv = (irq_flags >> (arg & 7)) & 1
                else:
                    wv = events[arg & 7]
                if wv != (arg >> 8):
                    stall = 1
                elif src == 2 and (arg >> 8):
                    out["irq_clr"] |= 1 << (arg & 7)
            elif op == 2:  # IN
                do_push = 0
                new_isr = 0
                cnt_sum = 0
                if coded and f_hi in (0, 3):
                    s_in = pins_in & 1
                    sym_done = 1
                    lbit = 0
                    if c["c_mode"] == 1:
                        lbit = c["c_nrzi_tog"] if s_in != s["rx_lvl"] else c["c_nrzi_tog"] ^ 1
                        nx["rx_lvl"] = s_in
                    elif c["c_mode"] == 2:
                        if not s["mph"]:
                            sym_done = 0
                            nx["mph"] = 1
                        else:
                            lbit = s_in ^ c["c_manch_pol"]
                            nx["mph"] = 0
                    else:
                        lbit = s_in
                    cnt_left = cb_cur
                    nx["cb_left"] = cnt_left
                    rpt = 1
                    if sym_done:
                        if stuff_now:
                            exp = (s["last_bit"] ^ 1) if c["c_stuff_mode"] else 0
                            if lbit != exp:
                                nx["stuff_err"] = 1
                            nx["run"] = 1 if c["c_stuff_mode"] else 0
                            nx["last_bit"] = lbit
                            if cnt_zero:
                                rpt = 0
                        else:
                            if c["c_stuff_mode"]:
                                nx["run"] = run_inc if lbit == s["last_bit"] else 1
                            else:
                                nx["run"] = run_inc if lbit else 0
                            nx["last_bit"] = lbit
                            if xmode:
                                nx["x"] = x_dec
                            else:
                                nx["cb_left"] = (cnt_left - 1) & 63
                            if cnt_last and not (s["stuff_on"] and c["c_stuff_n"] != 0 and nx["run"] == c["c_stuff_n"]):
                                rpt = 0
                            if f_hi == 0:
                                isr = s["isr"]
                                new_isr = ((lbit << 31) | (isr >> 1)) if c["in_shr"] else (((isr << 1) | lbit) & M32)
                                cnt_sum = sat_add(s["isr_cnt"], 1)
                                crc_step, crc_bit = 1, lbit
                                do_push = 1
                    nx["cb_active"] = rpt
                else:
                    new_isr = in_new
                    cnt_sum = sat_add(s["isr_cnt"], n6)
                    do_push = 1
                if do_push:
                    if c["autopush"] and cnt_sum >= push_th6:
                        if rxf.full:
                            stall = 1
                        else:
                            out["rxf_push"], out["rxf_wdata"] = 1, new_isr
                            nx["isr"], nx["isr_cnt"] = 0, 0
                    else:
                        nx["isr"], nx["isr_cnt"] = new_isr, cnt_sum
            elif op == 3:  # OUT
                if apull_need:
                    if txf.empty:
                        stall = 1
                    else:
                        out["txf_pop"] = 1
                nx["osr"], nx["osr_cnt"] = osr_e, osr_cnt_e
                if coded and f_hi in (0, 3):
                    dbit = (osr_e & 1) if c["out_shr"] else (osr_e >> 31) & 1
                    cnt_left = cb_cur
                    nx["cb_left"] = cnt_left
                    rpt = 1
                    sym_done = 1
                    if f_hi == 0:
                        need_stuff = stuff_now
                        stuff_bit = (s["last_bit"] ^ 1) if c["c_stuff_mode"] else 0
                        lbit = stuff_bit if need_stuff else dbit
                        if c["c_mode"] == 1:
                            sym = (s["tx_lvl"] ^ 1) if lbit == c["c_nrzi_tog"] else s["tx_lvl"]
                            nx["tx_lvl"] = sym
                        elif c["c_mode"] == 2:
                            if not s["mph"]:
                                sym = lbit if c["c_manch_pol"] else lbit ^ 1
                                sym_done = 0
                                nx["mph"] = 1
                            else:
                                sym = (lbit ^ 1) if c["c_manch_pol"] else lbit
                                nx["mph"] = 0
                        else:
                            sym = lbit
                        pw_en, pw_cnt, pw_val = 1, (2 if c["c_diff"] else 1), ((sym ^ 1) << 1) | sym
                    if sym_done:
                        if need_stuff:
                            nx["run"] = 1 if c["c_stuff_mode"] else 0
                            nx["last_bit"] = stuff_bit
                            if cnt_zero:
                                rpt = 0
                        else:
                            nx["osr"] = (osr_e >> 1) if c["out_shr"] else ((osr_e << 1) & M32)
                            nx["osr_cnt"] = sat_add(osr_cnt_e, 1)
                            crc_step, crc_bit = 1, dbit
                            if f_hi == 0:
                                if c["c_stuff_mode"]:
                                    nx["run"] = run_inc if dbit == s["last_bit"] else 1
                                else:
                                    nx["run"] = run_inc if dbit else 0
                                nx["last_bit"] = dbit
                            if xmode:
                                nx["x"] = x_dec
                            else:
                                nx["cb_left"] = (cnt_left - 1) & 63
                            if cnt_last and not (f_hi == 0 and s["stuff_on"] and c["c_stuff_n"] != 0
                                                 and nx["run"] == c["c_stuff_n"]):
                                rpt = 0
                    nx["cb_active"] = rpt
                else:
                    nx["osr"] = out_osr
                    nx["osr_cnt"] = sat_add(osr_cnt_e, n6)
                    if f_hi == 0:
                        pw_en, pw_val = 1, out_data
                    elif f_hi == 1:
                        nx["x"] = out_data
                    elif f_hi == 2:
                        nx["y"] = out_data
                    elif f_hi == 4:
                        pw_en, pw_dir, pw_val = 1, 1, out_data
                    elif f_hi == 5:
                        jump, target = 1, out_data & 63
                    elif f_hi == 6:
                        nx["isr"], nx["isr_cnt"] = out_data, n6
            elif op == 4:  # PUSH / PULL
                if not (arg >> 8):
                    if not ((arg >> 7) & 1 and s["isr_cnt"] < push_th6):
                        if rxf.full:
                            if (arg >> 6) & 1:
                                stall = 1
                            else:
                                nx["isr"], nx["isr_cnt"] = 0, 0
                        else:
                            out["rxf_push"], out["rxf_wdata"] = 1, s["isr"]
                            nx["isr"], nx["isr_cnt"] = 0, 0
                else:
                    if not ((arg >> 7) & 1 and s["osr_cnt"] < pull_th6):
                        if txf.empty:
                            if (arg >> 6) & 1:
                                stall = 1
                            else:
                                nx["osr"], nx["osr_cnt"] = s["x"], 0
                        else:
                            out["txf_pop"] = 1
                            nx["osr"], nx["osr_cnt"] = txf.head, 0
            elif op == 5:  # MOV
                mop = (arg >> 4) & 3
                mdata = [srcv, (~srcv) & M32, bitrev(srcv), (srcv + 1) & M32][mop]
                if f_hi == 0:
                    pw_en, pw_val = 1, mdata
                elif f_hi == 1:
                    nx["x"] = mdata
                elif f_hi == 2:
                    nx["y"] = mdata
                elif f_hi == 3:
                    nx["crc"] = mdata
                elif f_hi == 4:
                    pw_en, pw_dir, pw_val = 1, 1, mdata
                elif f_hi == 5:
                    jump, target = 1, mdata & 63
                elif f_hi == 6:
                    nx["isr"], nx["isr_cnt"] = mdata, 0
                else:
                    nx["osr"], nx["osr_cnt"] = mdata, 0
            elif op == 6:  # IRQ
                if (arg >> 4) & 1:
                    idx = (arg & 4) | (((arg & 3) + self.id) & 3)
                else:
                    idx = arg & 7
                if (arg >> 8) & 1:
                    out["irq_clr"] |= 1 << idx
                elif (arg >> 7) & 1:
                    if not s["irq_wait"]:
                        out["irq_set"] |= 1 << idx
                        nx["irq_wait"] = 1
                        stall = 1
                    elif (irq_flags >> idx) & 1:
                        stall = 1
                    else:
                        nx["irq_wait"] = 0
                else:
                    out["irq_set"] |= 1 << idx
            else:  # SET
                imm = arg & 63
                if f_hi == 0:
                    pw_en, pw_base, pw_cnt, pw_val = 1, c["set_base"], c["set_count"], imm
                elif f_hi == 1:
                    nx["x"] = imm
                elif f_hi == 2:
                    nx["y"] = imm
                elif f_hi == 3:
                    if imm & 1:
                        nx["crc"] = crc_wmask if c["crc_init1"] else 0
                        nx["crc_skip"] = c["crc_skip_cfg"]
                    if imm & 2:
                        nx.update(run=0, last_bit=0, mph=0, tx_lvl=c["c_idle"], rx_lvl=c["c_idle"])
                    if imm & 4:
                        nx["stuff_err"] = 0
                        nx["edge_flag"] = 0
                    if imm & 8:
                        nx["trap_armed"] = c["trap_en"]
                    if (imm >> 4) == 1:
                        nx["stuff_on"] = 0
                    if (imm >> 4) == 2:
                        nx["stuff_on"] = 1
                elif f_hi == 4:
                    pw_en, pw_dir, pw_base, pw_cnt, pw_val = 1, 1, c["set_base"], c["set_count"], imm

            if crc_step:
                if s["crc_skip"]:
                    nx["crc_skip"] = s["crc_skip"] - 1
                else:
                    crc = s["crc"]
                    nx["crc"] = ((crc << 1) & crc_wmask) ^ ((c["crc_poly"] & crc_wmask) if (crc_bit ^ crc_msb) else 0)

            if stall:
                for k in ("x", "y", "osr", "isr", "osr_cnt", "isr_cnt", "crc", "crc_skip", "cb_active", "cb_left",
                          "run", "last_bit", "tx_lvl", "rx_lvl", "mph", "stuff_err"):
                    nx[k] = s[k]
                out["txf_pop"] = out["rxf_push"] = 0
                pw_en = 0
            else:
                nx["delay_cnt"] = delay
                if jump:
                    nx["pc"] = target
                elif not rpt:
                    nx["pc"] = c["wrap_bot"] if s["pc"] == c["wrap_top"] else (s["pc"] + 1) & 63

        # ---- functional coverage
        if tick and trap_fire:
            self._cov("trap_fire")
        elif exec_:
            self._cov(f"op{op}" + ("_stall" if stall else ""))
            if op in (2, 3) and coded:
                self._cov(f"coded_{'in' if op == 2 else 'out'}_mode{c['c_mode']}")
                if rpt and not stall:
                    self._cov("coded_repeat")
            if need_stuff:
                self._cov("stuff_insert")
            if op == 2 and coded and nx["stuff_err"] and not s["stuff_err"]:
                self._cov("stuff_err")
            if out["rxf_push"]:
                self._cov("rx_push")
            if out["txf_pop"]:
                self._cov("tx_pop")
            if crc_step:
                self._cov("crc_step")
            if side_act:
                self._cov("sideset")
            if jump:
                self._cov("jump")
        if ts_edge_ev:
            self._cov("ts_edge")

        # ---- pin write unit + side-set
        sd_act = exec_ and side_act
        pw_m = rotl(nmask(pw_cnt), pw_base)
        pw_v = rotl(pw_val & M32, pw_base) & pw_m
        sd_m = rotl((1 << side_n) - 1 if side_n <= 4 else 0xF, c["side_base"])
        sd_v = rotl(side_v, c["side_base"]) & sd_m
        F = 0xFFFFF
        i_po = pw_m & F if (pw_en and not pw_dir) else 0
        i_pd = pw_m & F if (pw_en and pw_dir) else 0
        s_po = sd_m & F if (sd_act and not c["ss_pindir"]) else 0
        s_pd = sd_m & F if (sd_act and c["ss_pindir"]) else 0
        out["po_mask"] = i_po | s_po
        out["po_val"] = (pw_v & i_po & ~s_po) | (sd_v & s_po)
        out["pd_mask"] = i_pd | s_pd
        out["pd_val"] = (pw_v & i_pd & ~s_pd) | (sd_v & s_pd)
        out["_ts_edge_ev"] = ts_edge_ev
        out["_ts_pin_v"] = ts_pin_v
        out["_tick"] = tick
        return nx, out

    # ------------------------------------------------------------ sequential
    def edge(self, nx, out, en, restart, fifo_clr, div_sync, cfg_we, cfg_addr, cfg_wdata, rxf_pop, ts):
        c = self.c
        s_old = self.s
        s = dict(nx)
        s["ts_prev"] = out["_ts_pin_v"]
        if out["_ts_edge_ev"]:
            s["last_ts"] = ts
            s["delta_ts"] = (ts - s_old["last_ts"]) & 0xFFFF
        # divider
        if restart or div_sync:
            s["div_cnt"], s["div_acc"] = 0, 0
        elif en:
            if c["dpll_en"] and out["_ts_edge_ev"]:
                s["div_cnt"] = c["div_int"] >> 1
            elif out["_tick"]:
                tot = ((((c["div_int"] - 1) & 0xFFFF) << 8) + s_old["div_acc"] + c["div_frac"]) & 0xFFFFFF
                s["div_cnt"], s["div_acc"] = tot >> 8, tot & 0xFF
            else:
                s["div_cnt"] = (s_old["div_cnt"] - 1) & 0xFFFF
        else:
            s["div_cnt"], s["div_acc"] = s_old["div_cnt"], s_old["div_acc"]
        if restart:
            s.update(pc=c["start_addr"], isr_cnt=0, osr_cnt=32, delay_cnt=0, irq_wait=0, cb_active=0,
                     mph=0, run=0, last_bit=0, tx_lvl=c["c_idle"], rx_lvl=c["c_idle"],
                     stuff_on=c["c_stuff_en"], trap_armed=c["trap_en"])
        # FIFOs (use pre-edge state for full/empty)
        txf_push = cfg_we and cfg_addr == R.TXF
        self.txf.edge(fifo_clr, txf_push, cfg_wdata, out["txf_pop"])
        self.rxf.edge(fifo_clr, out["rxf_push"], out["rxf_wdata"], rxf_pop)
        # config writes
        if cfg_we:
            d = cfg_wdata
            a = cfg_addr
            if a == R.CLKDIV:
                c["div_int"], c["div_frac"] = d >> 16 & 0xFFFF, d >> 8 & 0xFF
            elif a == R.EXECCFG:
                c.update(wrap_bot=d & 63, wrap_top=d >> 6 & 63, start_addr=d >> 12 & 63, ss_count=d >> 18 & 7,
                         ss_en=d >> 21 & 1, ss_pindir=d >> 22 & 1, jmp_pin=d >> 23 & 31)
            elif a == R.SHIFTCFG:
                c.update(in_shr=d & 1, out_shr=d >> 1 & 1, autopush=d >> 2 & 1, autopull=d >> 3 & 1,
                         push_th=d >> 4 & 31, pull_th=d >> 9 & 31)
            elif a == R.PINCFG:
                c.update(out_base=d & 31, out_count=d >> 5 & 63, set_base=d >> 11 & 31, set_count=d >> 16 & 7,
                         side_base=d >> 19 & 31, in_base=d >> 24 & 31)
            elif a == R.CODECFG:
                c.update(c_mode=d & 3, c_nrzi_tog=d >> 2 & 1, c_manch_pol=d >> 3 & 1, c_stuff_en=d >> 4 & 1,
                         c_stuff_mode=d >> 5 & 1, c_stuff_n=d >> 6 & 15, c_diff=d >> 10 & 1, c_idle=d >> 11 & 1,
                         ts_pin=d >> 12 & 31, ts_edge=d >> 17 & 3, dpll_en=d >> 19 & 1)
                s.update(stuff_on=d >> 4 & 1, tx_lvl=d >> 11 & 1, rx_lvl=d >> 11 & 1)
            elif a == R.CRCCFG:
                c.update(crc_width=d & 63, crc_init1=d >> 6 & 1, crc_skip_cfg=d >> 7 & 63)
            elif a == R.CRCPOLY:
                c["crc_poly"] = d
            elif a == R.CRCVAL:
                s["crc"] = d
                s["crc_skip"] = c["crc_skip_cfg"]
            elif a == R.PATMASK:
                c["pat_mask"] = d & 0xFFFFF
            elif a == R.PATVAL:
                c["pat_val"] = d & 0xFFFFF
            elif a == R.TRAPCFG:
                c.update(trap_en=d & 1, trap_ev=d >> 1 & 7, trap_pol=d >> 4 & 1, trap_addr=d >> 5 & 63,
                         timeout=d >> 16 & 0xFFFF)
                s["trap_armed"] = d & 1
        for k in ("pc", "x", "y", "osr", "isr", "crc"):
            s[k] &= M32
        self.s = s

    def dbg(self):
        s = self.s
        return (s["osr_cnt"] << 24) | (s["stuff_on"] << 23) | (s["mph"] << 22) | (s["edge_flag"] << 21) \
            | (s["stuff_err"] << 20) | (s["trap_armed"] << 19) | (s["cb_active"] << 18) | (s["irq_wait"] << 17) \
            | (s["isr_cnt"] << 10) | (s["delay_cnt"] << 6) | s["pc"]


class Chip:
    """Whole-chip model below the SPI layer."""

    def __init__(self, n_engines=4, fifo_depth=2):
        self.eng = [Engine(i, fifo_depth) for i in range(n_engines)]
        self.imem = [0] * 64
        self.en = 0
        self.restart = self.fifo_clr = self.div_sync = 0
        self.imem_ptr = 0
        self.irq = 0
        self.gpio_out = 0
        self.gpio_oe = 0
        self.od = 0
        self.ts = 0
        self.ui_s1 = self.ui_s2 = 0x02
        self.uio_s1 = self.uio_s2 = 0
        self.cycle = 0

    # ------------------------------------------------------------ views
    @property
    def gpio_in(self):
        return ((self.gpio_out >> 13) << 13) | (((self.ui_s2 >> 3) & 0x1F) << 8) | (self.uio_s2 & 0xFF)

    def pads(self):
        """(uo_out[7:1] as GPIO13..19, uio_out, uio_oe) as driven now."""
        uo = (self.gpio_out >> 13) & 0x7F
        uio_out = self.gpio_out & 0xFF & ~self.od
        uio_oe = ((self.gpio_oe & ~self.od) | (self.od & ~self.gpio_out)) & 0xFF
        return uo, uio_out, uio_oe

    def read(self, addr):
        """Combinational host read (peek), returns (data, valid)."""
        if addr < 0x10:
            return {
                R.ID: R.LOOM_ID, R.CTRL: self.en, R.IMEM_ADDR: self.imem_ptr, R.IRQ: self.irq,
                R.GPIO: self.gpio_in, R.OD: self.od, R.OE: self.gpio_oe,
                R.FLEVEL: sum((len(e.txf.q) | len(e.rxf.q) << 4) << (8 * i) for i, e in enumerate(self.eng)),
            }.get(addr, 0), True
        n = (addr >> 4) - 1
        if n >= len(self.eng):
            return 0, True
        e = self.eng[n]
        r = addr & 0xF
        if r == R.RXF:
            return e.rxf.head, not e.rxf.empty
        if r == R.DBG:
            return e.dbg(), True
        if r == R.DBGX:
            return e.s["x"], True
        return 0, True

    # ------------------------------------------------------------ one clock edge
    def step(self, ui_in=0x02, uio_in=0, wr=None, rd_pop=None):
        """wr = (addr, data) presented this cycle; rd_pop = addr popped this cycle."""
        gpio_in = self.gpio_in
        combs = []
        for i, e in enumerate(self.eng):
            en = (self.en >> i) & 1 and not (self.restart >> i) & 1
            instr = self.imem[e.s["pc"]]
            combs.append(e.comb(en, instr, gpio_in, self.irq, self.ts))

        # top-level merges
        gout, goe, irq = self.gpio_out, self.gpio_oe, self.irq
        for nx, o in combs:
            gout = (gout & ~o["po_mask"]) | (o["po_val"] & o["po_mask"])
            goe = (goe & ~(o["pd_mask"] & 0xFF)) | (o["pd_val"] & o["pd_mask"] & 0xFF)
        for nx, o in combs:
            irq |= o["irq_set"]
        for nx, o in combs:
            irq &= ~o["irq_clr"]
        glob = wr is not None and wr[0] < 0x10
        if glob and wr[0] == R.IRQ:
            irq = (irq & ~(wr[1] & 0xFF)) | ((wr[1] >> 8) & 0xFF)
        if glob and wr[0] == R.GPIO:
            gout = wr[1] & 0xFFFFF
        if glob and wr[0] == R.OE:
            goe = wr[1] & 0xFF

        # engines' sequential update
        for i, (e, (nx, o)) in enumerate(zip(self.eng, combs)):
            cfg_we = wr is not None and (wr[0] >> 4) == i + 1
            e.edge(nx, o, (self.en >> i) & 1, (self.restart >> i) & 1, (self.fifo_clr >> i) & 1,
                   (self.div_sync >> i) & 1, cfg_we, wr[0] & 0xF if cfg_we else 0, wr[1] if cfg_we else 0,
                   rd_pop is not None and rd_pop == ((i + 1) << 4 | R.RXF), self.ts)

        # top sequential
        if glob and wr[0] == R.IMEM_DATA:
            self.imem[self.imem_ptr] = wr[1] & 0xFFFF
        self.ts = (self.ts + 1) & 0xFFFF
        self.irq = irq & 0xFF
        self.gpio_out = gout & 0xFFFFF
        self.gpio_oe = goe & 0xFF
        self.restart = self.fifo_clr = self.div_sync = 0
        if glob:
            a, d = wr
            if a == R.CTRL:
                self.en, self.restart, self.fifo_clr, self.div_sync = d & 15, d >> 4 & 15, d >> 8 & 15, d >> 12 & 15
            elif a == R.IMEM_ADDR:
                self.imem_ptr = d & 63
            elif a == R.IMEM_DATA:
                self.imem_ptr = (self.imem_ptr + 1) & 63
            elif a == R.OD:
                self.od = d & 0xFF
        self.ui_s2, self.ui_s1 = self.ui_s1, ui_in & 0xFF
        self.uio_s2, self.uio_s1 = self.uio_s1, uio_in & 0xFF
        self.cycle += 1

    # ------------------------------------------------------------ convenience (instant host access)
    def write(self, addr, data, ui_in=0x02, uio_in=0):
        self.step(ui_in, uio_in, wr=(addr, data & M32))

    def pop(self, n):
        addr = (n + 1) << 4 | R.RXF
        d, v = self.read(addr)
        self.step(rd_pop=addr if v else None)
        return d if v else None

    def load(self, prog, origin=0):
        self.write(R.IMEM_ADDR, origin)
        for w in prog.relocated(origin):
            self.write(R.IMEM_DATA, w)

    def configure(self, n, cfg):
        for r, v in cfg.regs().items():
            self.write(R.ENG(n, r), v)
        self.write(R.ENG(n, R.CRCVAL), cfg.crc_init_value())
