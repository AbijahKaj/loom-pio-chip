"""Independent, cycle-level device models used as protocol references.

Every model implements:
    drives() -> {gpio: 0 | 1 | None}   what it drives *this* cycle (None = released)
    tick(cycle, levels)                observe resolved pad levels, update state
None of these share code with the engine or its programs.
"""
import zlib


# ====================================================================== UART
class UartMonitor:
    def __init__(self, gpio, bit_cycles):
        self.g, self.bc = gpio, bit_cycles
        self.bytes = []
        self.errors = 0
        self.state = "idle"
        self.t = 0
        self.prev = 1
        self.high_run = 0          # only arm after a full bit time of idle-high line

    def drives(self):
        return {}

    def tick(self, cyc, lv):
        v = lv[self.g]
        self.high_run = self.high_run + 1 if v else 0
        if self.state == "idle":
            if self.prev == 1 and v == 0 and self.armed:
                self.state, self.t, self.bits = "frame", 0, []
        else:
            self.t += 1
            # sample at the middle of each bit: bit k at (k + 0.5) * bc after the edge
            k, r = divmod(self.t, self.bc)
            if r == self.bc // 2:
                if k == 0:
                    if v != 0:
                        self.state = "idle"
                elif 1 <= k <= 8:
                    self.bits.append(v)
                elif k == 9:
                    if v == 1:
                        self.bytes.append(sum(b << i for i, b in enumerate(self.bits)))
                    else:
                        self.errors += 1
                    self.state = "idle"
        if self.high_run >= self.bc:
            self.armed = True
        self.prev = v

    armed = False


class UartDriver:
    def __init__(self, gpio, bit_cycles, data=(), stop_ok=True, gap=0):
        self.g, self.bc = gpio, bit_cycles
        self.queue = []
        self.level = 1
        self.t = 0
        self.gap = gap
        for b in data:
            self.send(b, stop_ok)

    def send(self, byte, stop_ok=True):
        bits = [0] + [(byte >> i) & 1 for i in range(8)] + [1 if stop_ok else 0]
        for b in bits:
            self.queue += [b] * self.bc
        self.queue += [1] * (self.bc * 2 + self.gap)

    def drives(self):
        return {self.g: self.level}

    def tick(self, cyc, lv):
        self.level = self.queue.pop(0) if self.queue else 1

    @property
    def idle(self):
        return not self.queue


# ====================================================================== SPI
class SpiTarget:
    """SPI peripheral model (any mode), MSB first, fixed frame length; no CS (frames by count)."""

    def __init__(self, sck, mosi, miso, cpol=0, cpha=0, bits=8, responses=()):
        self.sck, self.mosi, self.miso = sck, mosi, miso
        self.cpol, self.cpha, self.nbits = cpol, cpha, bits
        self.responses = list(responses)
        self.received = []
        self.prev_sck = cpol
        self.cnt = 0
        self.rx = 0
        self.tx = None
        self.out = 1
        self._load()

    def _load(self):
        self.tx = self.responses.pop(0) if self.responses else (1 << self.nbits) - 1
        self.txbit = self.nbits - 1
        if self.cpha == 0:
            self.out = (self.tx >> self.txbit) & 1

    def drives(self):
        return {self.miso: self.out}

    def tick(self, cyc, lv):
        s = lv[self.sck]
        if s != self.prev_sck:
            leading = (s != self.cpol)
            sample = leading if self.cpha == 0 else not leading
            if sample:
                self.rx = (self.rx << 1) | lv[self.mosi]
                self.cnt += 1
                if self.cnt == self.nbits:
                    self.received.append(self.rx)
                    self.rx, self.cnt = 0, 0
                    if self.cpha == 0:
                        self._next_after_frame = True
            else:
                # shift-out edge
                if self.cpha == 0:
                    if getattr(self, "_next_after_frame", False):
                        self._next_after_frame = False
                        self._load()
                    else:
                        self.txbit -= 1
                        self.out = (self.tx >> self.txbit) & 1
                else:
                    if self.txbit < 0:
                        self._load()
                    self.out = (self.tx >> self.txbit) & 1
                    self.txbit -= 1
        self.prev_sck = s


class SpiController:
    """SPI controller model, mode 0, MSB first, drives CS_N/SCK/MOSI, samples MISO."""

    def __init__(self, cs, sck, mosi, miso, half_cycles, frames=(), bits=8, gap=0):
        self.cs, self.sck, self.mosi, self.miso = cs, sck, mosi, miso
        self.half, self.bits, self.gap = half_cycles, bits, gap
        self.script = []
        self.received = []
        self.lv = {cs: 1, sck: 0, mosi: 1}
        self.t = 0
        for f in frames:
            self.transfer(f)

    def transfer(self, frame_bytes, partial_bits=None):
        """Queue a CS-framed transfer of several bytes (optionally aborted after partial_bits)."""
        s = [("cs", 0), ("wait", self.half * 2)]
        nbits = len(frame_bytes) * self.bits if partial_bits is None else partial_bits
        for k in range(nbits):
            byte = frame_bytes[k // self.bits]
            bit = (byte >> (self.bits - 1 - k % self.bits)) & 1
            s += [("mosi", bit), ("wait", self.half), ("sck", 1), ("sample", None), ("wait", self.half),
                  ("sck", 0)]
        s += [("wait", self.half), ("cs", 1), ("wait", self.half * 4 + self.gap), ("flush", None)]
        self.script += s

    def drives(self):
        return dict(self.lv)

    @property
    def idle(self):
        return not self.script

    def tick(self, cyc, lv):
        self._rx = getattr(self, "_rx", [])
        while self.script:
            op, arg = self.script[0]
            if op == "wait":
                if arg > 1:
                    self.script[0] = ("wait", arg - 1)
                    return
                self.script.pop(0)
                return
            self.script.pop(0)
            if op == "cs":
                self.lv[self.cs] = arg
            elif op == "sck":
                self.lv[self.sck] = arg
            elif op == "mosi":
                self.lv[self.mosi] = arg
            elif op == "sample":
                self._rx.append(lv[self.miso])
            elif op == "flush":
                bits = self._rx
                self.received.append([sum(b << (self.bits - 1 - i) for i, b in enumerate(bits[k:k + self.bits]))
                                      for k in range(0, len(bits) - len(bits) % self.bits, self.bits)])
                self._rx = []


# ====================================================================== I2C
class I2CTarget:
    """I2C target with a 24Cxx-style register pointer, open drain, optional clock stretching.
    Write: S addr+W reg data... P.  Read: S addr+W reg Sr addr+R data(ACK)... data(NACK) P."""

    def __init__(self, sda, scl, addr=0x50, mem=None, stretch=0):
        self.sda, self.scl, self.addr = sda, scl, addr
        self.mem = bytearray(mem or bytes(256))
        self.stretch = stretch
        self.sda_drv = None
        self.scl_drv = None
        self.prev_sda, self.prev_scl = 1, 1
        self.state = "idle"
        self.ptr = 0
        self.log = []
        self.stretch_left = 0
        self.max_stretch_seen = 0

    def drives(self):
        return {self.sda: self.sda_drv, self.scl: self.scl_drv}

    def _drive_bit(self):
        bit = (self.mem[self.ptr] >> self.bit) & 1
        self.sda_drv = None if bit else 0
        self.bit -= 1

    def tick(self, cyc, lv):
        sda, scl = lv[self.sda], lv[self.scl]
        if self.stretch_left:
            self.stretch_left -= 1
            if self.stretch_left == 0:
                self.scl_drv = None
        if scl and self.prev_scl and sda != self.prev_sda and self.sda_drv is None:
            if not sda:
                self.log.append("S")
                self.state, self.purpose, self.cnt, self.byte = "rx", "addr", 0, 0
            else:
                self.log.append("P")
                self.state = "idle"
        elif scl and not self.prev_scl:                       # rising edge: sample
            if self.state == "rx":
                self.byte = ((self.byte << 1) | sda) & 0xFF
                self.cnt += 1
            elif self.state == "tx_ack":
                self.master_ack = (sda == 0)
        elif not scl and self.prev_scl:                       # falling edge: drive
            if self.stretch and self.state != "idle":
                self.scl_drv = 0
                self.stretch_left = self.stretch
            if self.state == "rx" and self.cnt == 8:
                b = self.byte
                if self.purpose == "addr":
                    if (b >> 1) != self.addr:
                        self.log.append(f"N{b:02x}")
                        self.state = "idle"
                    else:
                        self.log.append(f"A{b:02x}")
                        self.rw = b & 1
                        self.sda_drv, self.state = 0, "acking"
                elif self.purpose == "reg":
                    self.ptr = b
                    self.log.append(f"R{b:02x}")
                    self.sda_drv, self.state = 0, "acking"
                else:
                    self.mem[self.ptr] = b
                    self.log.append(f"W{b:02x}")
                    self.ptr = (self.ptr + 1) & 0xFF
                    self.sda_drv, self.state = 0, "acking"
            elif self.state == "acking":
                self.sda_drv = None
                if self.purpose == "addr" and self.rw:
                    self.state, self.bit = "tx", 7
                    self.log.append(f"D{self.mem[self.ptr]:02x}")
                    self._drive_bit()
                else:
                    self.purpose = "reg" if self.purpose == "addr" else "data"
                    self.state, self.cnt, self.byte = "rx", 0, 0
            elif self.state == "tx":
                if self.bit >= 0:
                    self._drive_bit()
                else:
                    self.sda_drv, self.state = None, "tx_ack"
            elif self.state == "tx_ack":
                if self.master_ack:
                    self.ptr = (self.ptr + 1) & 0xFF
                    self.state, self.bit = "tx", 7
                    self.log.append(f"D{self.mem[self.ptr]:02x}")
                    self._drive_bit()
                else:
                    self.sda_drv, self.state = None, "idle"
        self.prev_sda, self.prev_scl = sda, scl


# ====================================================================== USB low speed
def usb_crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def usb_packet_bits(pid, payload=None):
    """Raw (pre-stuffing, pre-NRZI) bits: SYNC, PID, payload, CRC16 if payload given."""
    data = [0x80, (pid & 0xF) | ((~pid & 0xF) << 4)]
    if payload is not None:
        crc = usb_crc16(payload)
        data += list(payload) + [crc & 0xFF, crc >> 8]
    return [(b >> i) & 1 for b in data for i in range(8)]


def usb_stuff(bits):
    out, run = [], 0
    for b in bits:
        out.append(b)
        run = run + 1 if b else 0
        if run == 6:
            out.append(0)
            run = 0
    return out


class UsbLsMonitor:
    """Decodes low-speed USB packets from D-/D+ (J = D- high). Recovers timing from edges."""

    def __init__(self, dm, dp, bit_cycles):
        self.dm, self.dp, self.bc = dm, dp, bit_cycles
        self.packets = []
        self.errors = []
        self.state = "idle"
        self.prev = (1, 0)

    def drives(self):
        return {}

    def tick(self, cyc, lv):
        cur = (lv[self.dm], lv[self.dp])
        if self.state == "idle":
            if self.prev == (1, 0) and cur == (0, 1):        # J -> K: SYNC starts
                self.state, self.t, self.syms = "rx", 0, []
                self.last_edge = 0
        elif self.state == "rx":
            self.t += 1
            if cur != self.prev:
                self.last_edge = self.t
            bc = int(round(self.bc))     # edges re-align the sampling, so a rounded bit time is fine
            if (self.t - self.last_edge) % bc == bc // 2:
                if cur == (0, 0):
                    self.state = "eop"
                    self.se0 = 1
                else:
                    self.syms.append(cur[0])
        elif self.state == "eop":
            self.t += 1
            if cur == (1, 0):
                self._finish()
                self.state = "idle"
        self.prev = cur

    def _finish(self):
        # NRZI decode (prev symbol before SYNC was J = 1)
        bits, lvl = [], 1
        for s in self.syms:
            bits.append(1 if s == lvl else 0)
            lvl = s
        # de-stuff
        out, run, i = [], 0, 0
        while i < len(bits):
            b = bits[i]
            out.append(b)
            run = run + 1 if b else 0
            i += 1
            if run == 6:
                if i < len(bits) and bits[i] != 0:
                    self.errors.append("stuff")
                i += 1
                run = 0
        nbytes = len(out) // 8
        data = [sum(out[8 * k + i] << i for i in range(8)) for k in range(nbytes)]
        if len(out) % 8:
            self.errors.append(f"bits={len(out)}")
        self.packets.append(data)


class UsbLsDriver:
    """Transmits low-speed USB packets onto D-/D+ (with optional clock error in ppm)."""

    def __init__(self, dm, dp, bit_cycles, ppm=0):
        self.dm, self.dp = dm, dp
        self.bc = bit_cycles * (1 + ppm * 1e-6)
        self.queue = []
        self.lv = None
        self.t = 0.0

    def send(self, pid, payload=None, idle_bits=8, corrupt_bit=None):
        bits = usb_stuff(usb_packet_bits(pid, payload))
        if corrupt_bit is not None:
            bits[corrupt_bit] ^= 1
        syms, lvl = [], 1               # idle J = D- high
        for b in bits:
            if b == 0:
                lvl ^= 1
            syms.append(lvl)
        seq = [(1, 0)] * idle_bits + [(s, s ^ 1) for s in syms] + [(0, 0), (0, 0), (1, 0)]
        self.queue += seq

    def drives(self):
        if self.lv is None:
            return {self.dm: None, self.dp: None}
        return {self.dm: self.lv[0], self.dp: self.lv[1]}

    @property
    def idle(self):
        return not self.queue

    def tick(self, cyc, lv):
        if not self.queue:
            self.lv = None
            return
        self.t += 1
        if self.lv is None:
            self.lv = self.queue[0]
            self.t = 0
        if self.t >= self.bc:
            self.t -= self.bc
            self.queue.pop(0)
            self.lv = self.queue[0] if self.queue else None


# ====================================================================== Manchester / 10BASE-T
def eth_frame(dst, src, ethertype, payload):
    body = bytes(dst) + bytes(src) + ethertype.to_bytes(2, "big") + bytes(payload)
    body += bytes(max(0, 60 - len(body)))
    fcs = zlib.crc32(body) & 0xFFFFFFFF
    return bytes([0x55] * 7 + [0xD5]) + body + fcs.to_bytes(4, "little")


class ManchesterMonitor:
    """IEEE 802.3 Manchester decoder for TD+ ('1' = low->high mid-bit), half-bit = hc cycles."""

    def __init__(self, gpio, half_cycles, gpio_n=None):
        self.g, self.gn, self.hc = gpio, gpio_n, half_cycles
        self.samples = []
        self.frames = []
        self.diff_errors = 0
        self.active = False
        self.idle_cnt = 0

    def drives(self):
        return {}

    def tick(self, cyc, lv):
        v = lv[self.g]
        if self.gn is not None and lv[self.gn] == v and self.active:
            self.diff_errors += 0   # both low at idle is legal; checked separately
        self.samples.append(v)

    def decode(self):
        """Decode all frames from the captured sample stream (offline, timing-based)."""
        s = self.samples
        frames = []
        i = 0
        n = len(s)
        while i < n:
            # find first rising edge (first preamble bit '1' = low->high at its middle)
            while i + 1 < n and not (s[i] == 0 and s[i + 1] == 1):
                i += 1
            if i + 1 >= n:
                break
            mid = i + 1                       # mid-bit transition of bit 0
            bits = []
            t = mid
            while True:
                # value just after the mid-bit transition = bit value
                if t + 1 >= n:
                    break
                b = s[t]
                bits.append(b)
                # check a real transition happened at t
                if s[t - 1] == b:
                    bits.pop()
                    break
                t += 2 * self.hc
            frames.append(bits)
            i = t + 1
        return frames


# ====================================================================== WS2812
class Ws2812Monitor:
    def __init__(self, gpio, cycles_per_us):
        self.g, self.cpu = gpio, cycles_per_us
        self.high = 0
        self.bits = []
        self.prev = 0

    def drives(self):
        return {}

    def tick(self, cyc, lv):
        v = lv[self.g]
        if v:
            self.high += 1
        if self.prev and not v:
            us = self.high / self.cpu
            self.bits.append(1 if us > 0.55 else 0)
            self.highs = getattr(self, "highs", []) + [us]
            self.high = 0
        self.prev = v


# ====================================================================== pulses / PS2
class PulseGen:
    def __init__(self, gpio, intervals, start=200, level=0):
        self.g = gpio
        self.times = []
        t = start
        for d in intervals:
            t += d
            self.times.append(t)
        self.level = level

    def drives(self):
        return {self.g: self.level}

    def tick(self, cyc, lv):
        if self.times and cyc >= self.times[0]:
            self.times.pop(0)
            self.level ^= 1


class PS2Device:
    """PS/2 device sending scan codes: 11-bit frames, ~12.5 kHz clock, data changes while CLK high."""

    def __init__(self, clk, data, half_cycles, codes=()):
        self.clk, self.data, self.h = clk, data, half_cycles
        self.seq = []
        self.lv = {clk: 1, data: 1}
        for c in codes:
            self.send(c)

    def send(self, code):
        par = 1 ^ (bin(code).count("1") & 1)
        bits = [0] + [(code >> i) & 1 for i in range(8)] + [par, 1]
        for b in bits:
            self.seq += [("d", b)] + [("w", None)] * (self.h // 2) + [("c", 0)] + [("w", None)] * self.h \
                + [("c", 1)] + [("w", None)] * (self.h - self.h // 2)
        self.seq += [("w", None)] * (self.h * 6)

    def drives(self):
        return dict(self.lv)

    def tick(self, cyc, lv):
        while self.seq:
            op, a = self.seq.pop(0)
            if op == "w":
                return
            self.lv[self.clk if op == "c" else self.data] = a


# ====================================================================== CAN
def can_crc15(bits):
    crc = 0
    for b in bits:
        fb = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if fb:
            crc ^= 0x4599
    return crc


def can_frame_bits(ident, data):
    """Unstuffed bits from SOF to the end of the CRC field (standard data frame)."""
    bits = [0]                                              # SOF
    bits += [(ident >> (10 - i)) & 1 for i in range(11)]    # ID
    bits += [0, 0, 0]                                       # RTR, IDE, r0
    bits += [(len(data) >> (3 - i)) & 1 for i in range(4)] # DLC
    for d in data:
        bits += [(d >> (7 - i)) & 1 for i in range(8)]
    crc = can_crc15(bits)
    bits += [(crc >> (14 - i)) & 1 for i in range(15)]
    return bits


def can_stuff(bits):
    out, run, last = [], 0, None
    for b in bits:
        out.append(b)
        if b == last:
            run += 1
        else:
            run, last = 1, b
        if run == 5:
            out.append(1 - b)
            run, last = 1, 1 - b
    return out


class BitSampler:
    """Samples a pin at the middle of each bit after the first falling edge (NRZ)."""

    def __init__(self, gpio, bit_cycles, nbits):
        self.g, self.bc, self.n = gpio, bit_cycles, nbits
        self.bits = []
        self.t = None
        self.prev = 1
        self.high_run = 0

    def drives(self):
        return {}

    def tick(self, cyc, lv):
        v = lv[self.g]
        self.high_run = self.high_run + 1 if v else 0
        if self.t is None:
            if self.prev == 1 and v == 0 and self.high_run == 0 and getattr(self, "armed", False):
                self.t = 0
            if self.high_run >= self.bc:
                self.armed = True
        else:
            self.t += 1
            if self.t % self.bc == self.bc // 2 and len(self.bits) < self.n:
                self.bits.append(v)
        self.prev = v


class LevelDriver:
    """Drives a sequence of line levels, one per (bit_cycles * (1 + ppm)) cycles; idles at `idle`."""

    def __init__(self, gpio, unit_cycles, idle=1, ppm=0):
        self.g, self.idle = gpio, idle
        self.unit = unit_cycles * (1 + ppm * 1e-6)
        self.queue = []
        self.t = 0.0
        self.level = idle

    def send(self, levels):
        self.queue += list(levels)

    @property
    def done(self):
        return not self.queue

    def drives(self):
        return {self.g: self.level}

    def tick(self, cyc, lv):
        if not self.queue:
            self.level = self.idle
            return
        if self.level == self.idle and self.t == 0.0 and not getattr(self, "_started", False):
            self._started = True
        self.level = self.queue[0]
        self.t += 1
        if self.t >= self.unit:
            self.t -= self.unit
            self.queue.pop(0)


def manchester_levels(bits):
    """IEEE 802.3: '1' = low then high, '0' = high then low (two half-bit levels per bit)."""
    out = []
    for b in bits:
        out += [0, 1] if b else [1, 0]
    return out
