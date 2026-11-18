"""Protocol scenarios, written once and run on both the ISS (test/run_iss.py) and the
RTL (test/test_protocols.py). Each takes a Board and asserts against an
independent device model from models.py."""
import random

import models as M
from board import R, prog

CODE = R


def pack_bits_lsb(bits):
    """Pack a bit list into 32-bit words, first bit at bit 0."""
    words = []
    for i in range(0, len(bits), 32):
        chunk = bits[i:i + 32]
        words.append(sum(b << k for k, b in enumerate(chunk)))
    return words


def pack_bits_msb(bits):
    """Pack a bit list into 32-bit words, first bit at bit 31."""
    words = []
    for i in range(0, len(bits), 32):
        chunk = bits[i:i + 32]
        words.append(sum(b << (31 - k) for k, b in enumerate(chunk)))
    return words


# ====================================================================== UART
async def uart_loopback(b, bit_cycles=None):
    """UART TX on GPIO13 checked by a model receiver; UART RX on GPIO8 fed by a model transmitter."""
    bit_cycles = bit_cycles or (120 if b.slow_host else 32)     # 500 kbaud with the real SPI host
    tx_mon = b.add(M.UartMonitor(13, bit_cycles))
    data_in = [0x00, 0xFF, 0x5A, 0x81, 0x37]
    drv = b.add(M.UartDriver(8, bit_cycles, gap=3))
    ptx, prx = prog("uart_tx"), prog("uart_rx")
    await b.load(ptx, 0)
    await b.load(prx, 8, engine=1)
    await b.configure(0, ptx.config(0, clkdiv=bit_cycles / 4, out_base=13, out_count=1, sideset_base=13,
                                    out_shift_right=True))
    await b.configure(1, prx.config(8, clkdiv=bit_cycles / 8, in_base=8, jmp_pin=8, in_shift_right=True))
    await b.write(R.GPIO, 1 << 13)          # idle high before the engine takes over
    await b.enable(0b0011)
    out = [0x55, 0xA3, 0x00, 0xFF, 0x42]
    await b.push_wait(0, *out)
    for d in data_in:
        drv.send(d)
    rx = await b.collect(1, len(data_in), max_cycles=bit_cycles * 12 * (len(data_in) + 4))
    await b.wait_for(lambda: len(tx_mon.bytes) >= len(out), max_cycles=bit_cycles * 12 * 8)
    assert tx_mon.bytes == out, tx_mon.bytes
    assert tx_mon.errors == 0
    got = [w >> 24 for w in rx]
    assert got == data_in, [hex(x) for x in got]
    # framing error -> IRQ flag 4 (relative to engine 1 -> flag 5)
    drv.send(0x99, stop_ok=False)
    await b.wait_for(lambda: drv.idle, max_cycles=bit_cycles * 20)
    await b.cycles(bit_cycles * 2)
    assert (await b.irq()) & (1 << 5), "framing error not flagged"
    return {"tx": out, "rx": got}


# ====================================================================== SPI controller
async def spi_controller(b, mode, clkdiv=3):
    cpol, cpha = mode >> 1, mode & 1
    p = prog("spi_cpha1" if cpha else "spi_cpha0", CPOL=cpol)
    resp = [0x3C, 0xA5, 0x0F, 0xF0]
    await b.write(R.GPIO, cpol << 14)      # SCK idle level before the target model attaches
    await b.cycles(4)
    tgt = b.add(M.SpiTarget(sck=14, mosi=15, miso=9, cpol=cpol, cpha=cpha, responses=resp))
    await b.load(p, 0)
    await b.configure(0, p.config(0, clkdiv=clkdiv, sideset_base=14, out_base=15, out_count=1, in_base=9,
                                  autopull=True, autopush=True, pull_thresh=8, push_thresh=8))
    await b.write(R.GPIO, cpol << 14)
    await b.enable(0b0001)
    tx = [0x81, 0x7E, 0x55, 0xC3]
    await b.push_wait(0, *[d << 24 for d in tx])
    rx = await b.collect(0, len(tx), max_cycles=4000 * clkdiv)
    assert tgt.received == tx, (mode, [hex(x) for x in tgt.received])
    assert [w & 0xFF for w in rx] == resp, (mode, [hex(w & 0xFF) for w in rx])
    return {"mode": mode, "mosi": tx, "miso": resp}


# ====================================================================== SPI target
async def spi_target(b, half=12):
    p = prog("spi_target", CS=10, SCK=9)
    ctl = b.add(M.SpiController(cs=10, sck=9, mosi=8, miso=13, half_cycles=half))
    await b.load(p, 0)
    await b.configure(0, p.config(0, in_base=8, out_base=13, out_count=1, autopush=True, push_thresh=8,
                                  pat_mask=1 << 10, pat_val=1 << 10, trap_en=True,
                                  trap_event=R.EV_PATTERN, trap_pol=1, trap_addr=0))
    await b.enable(0b0001)
    await b.push(0, 0xA1 << 24, 0xB2 << 24)
    await b.cycles(20)
    ctl.transfer([0x12, 0x34])
    await b.wait_for(lambda: ctl.idle, max_cycles=half * 80)
    rx = await b.drain(0)
    assert [w & 0xFF for w in rx] == [0x12, 0x34], [hex(w) for w in rx]
    assert ctl.received[-1] == [0xA1, 0xB2], ctl.received
    # aborted transfer (CS released after 5 bits) must not corrupt the next one
    ctl.transfer([0xFF], partial_bits=5)
    await b.wait_for(lambda: ctl.idle, max_cycles=half * 40)
    assert await b.drain(0) == []
    await b.push(0, 0x5C << 24)
    ctl.transfer([0x9D, 0x01])
    await b.wait_for(lambda: ctl.idle, max_cycles=half * 80)
    rx = await b.drain(0)
    assert [w & 0xFF for w in rx] == [0x9D, 0x01], [hex(w) for w in rx]
    assert ctl.received[-1] == [0x5C, 0xFF], ctl.received
    return {"rx": [0x12, 0x34, 0x9D, 0x01]}


# ====================================================================== I2C
I2C_BYTE, I2C_START, I2C_STOP, I2C_RSTART = 0, 1 << 9, 2 << 9, 3 << 9


def i2c_w(byte):
    return I2C_BYTE | (byte << 1) | 1


def i2c_r(ack=True):
    return I2C_BYTE | (0xFF << 1) | (0 if ack else 1)


async def i2c_controller(b, clkdiv=4, stretch=0):
    p = prog("i2c_ctrl", SCL=1)
    b.pullups |= {0, 1}
    mem = bytes(range(0x40, 0x40 + 256 - 0x40)) + bytes(0x40)
    tgt = b.add(M.I2CTarget(sda=0, scl=1, addr=0x50, mem=mem, stretch=stretch))
    await b.load(p, 0)
    await b.configure(0, p.config(0, clkdiv=clkdiv, sideset_base=1, set_base=0, set_count=1, out_base=0,
                                  out_count=1, in_base=0))
    await b.write(R.GPIO, 0b11)       # both released
    await b.write(R.OD, 0b11)         # open drain on SDA/SCL
    await b.enable(0b0001)
    # write 3 bytes at register 0x10
    cmds = [I2C_START, i2c_w(0xA0), i2c_w(0x10), i2c_w(0xDE), i2c_w(0xAD), i2c_w(0xBE), I2C_STOP]
    await b.push_wait(0, *cmds)
    rx = await b.collect(0, 5, max_cycles=clkdiv * 11 * 10 * 12 + stretch * 100)
    assert all((w & 1) == 0 for w in rx), f"missing ACK: {[hex(w) for w in rx]}"
    await b.cycles(clkdiv * 40 + stretch * 4)
    assert bytes(tgt.mem[0x10:0x13]) == b"\xDE\xAD\xBE", tgt.log
    # read back 3 bytes with a repeated start
    cmds = [I2C_START, i2c_w(0xA0), i2c_w(0x10), I2C_RSTART, i2c_w(0xA1), i2c_r(), i2c_r(), i2c_r(False),
            I2C_STOP]
    await b.push_wait(0, *cmds)
    rx = await b.collect(0, 6, max_cycles=clkdiv * 11 * 10 * 14 + stretch * 100)
    assert all((w & 1) == 0 for w in rx[:3]), [hex(w) for w in rx]
    data = [(w >> 1) & 0xFF for w in rx[3:]]
    assert data == [0xDE, 0xAD, 0xBE], [hex(d) for d in data]
    assert (rx[5] & 1) == 1  # our NACK as sampled back
    # address NACK
    await b.push_wait(0, I2C_START, i2c_w(0xB0), I2C_STOP)
    rx = await b.collect(0, 1, max_cycles=clkdiv * 11 * 40 + stretch * 40)
    assert rx[0] & 1 == 1, "expected NACK from absent target"
    await b.cycles(clkdiv * 40)
    return {"log": tgt.log}


# ====================================================================== USB low speed
async def usb_ls_tx(b, bit_cycles=40):
    p = prog("usb_ls_tx")
    mon = b.add(M.UsbLsMonitor(dm=2, dp=3, bit_cycles=bit_cycles))
    await b.load(p, 0)
    await b.configure(0, p.config(0, clkdiv=bit_cycles, out_base=2, out_count=2, set_base=2, set_count=2,
                                  code_mode=R.CODE_NRZI, nrzi_toggle_on=0, stuff_en=True, stuff_mode=0,
                                  stuff_n=6, diff=True, idle_level=1, out_shift_right=True, autopull=True))
    await b.enable(0b0001)
    packets = [(0xC3, bytes([0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 0x40, 0x00])),   # DATA0 setup
               (0x4B, bytes([0xFF, 0xFF, 0xFF, 0x7F, 0x3F])),                     # stuffing-heavy
               (0xD2, None)]                                                      # ACK handshake
    for pid, payload in packets:
        bits = M.usb_packet_bits(pid, payload)
        await b.push_wait(0, len(bits), *pack_bits_lsb(bits))
    await b.wait_for(lambda: len(mon.packets) >= len(packets), max_cycles=int(bit_cycles * 400))
    for (pid, payload), got in zip(packets, mon.packets):
        exp = [0x80, (pid & 0xF) | ((~pid & 0xF) << 4)]
        if payload is not None:
            crc = M.usb_crc16(payload)
            exp += list(payload) + [crc & 0xFF, crc >> 8]
        assert got == exp, (hex(pid), [hex(x) for x in got])
    assert not mon.errors, mon.errors
    return {"packets": len(packets)}


def crc_serial_msb(bits, poly, width, init):
    crc = init
    mask = (1 << width) - 1
    for bt in bits:
        fb = bt ^ ((crc >> (width - 1)) & 1)
        crc = (crc << 1) & mask
        if fb:
            crc ^= poly
    return crc


async def usb_ls_rx(b, bit_cycles=40, ppm=0):
    p = prog("usb_ls_rx")
    drv = b.add(M.UsbLsDriver(dm=2, dp=3, bit_cycles=bit_cycles, ppm=ppm))
    b.pullups |= {2}       # 1.5k pull-up on D- for a low-speed device: idle J
    await b.load(p, 0)
    await b.configure(0, p.config(0, clkdiv=bit_cycles, in_base=2, code_mode=R.CODE_NRZI, nrzi_toggle_on=0,
                                  stuff_en=True, stuff_mode=0, stuff_n=6, idle_level=0, ts_pin=2,
                                  ts_edge=R.EDGE_BOTH, dpll=True, crc_width=16, crc_poly=0x8005,
                                  crc_init_ones=True, crc_skip=8, in_shift_right=True, autopush=True,
                                  pat_mask=0b1100, pat_val=0, trap_en=True, trap_event=R.EV_PATTERN,
                                  trap_pol=1, trap_addr=p.addr("eop")))
    await b.enable(0b0001)
    results = []
    cases = [(0xC3, bytes([0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 0x12, 0x00]), None),
             (0x4B, bytes([0xFF, 0xFF, 0x7E, 0x3F, 0xFF, 0x01]), None),
             (0xC3, bytes([0x11, 0x22, 0x33]), 30)]                # corrupted bit -> bad CRC
    for pid, payload, corrupt in cases:
        drv.send(pid, payload, corrupt_bit=corrupt)
        words = []
        while True:
            words += await b.drain(0)
            if (await b.irq()) & 1:
                words += await b.drain(0)
                await b.clear_irq(1)
                break
            await b.cycles(int(bit_cycles * 4))
        assert len(words) >= 3, words
        full, partial, status, crc = words[:-3], words[-3], words[-2], words[-1]
        nbits = status & 63
        bits = [(w >> i) & 1 for w in full for i in range(32)]
        bits += [((partial >> (32 - nbits)) >> i) & 1 for i in range(nbits)] if nbits else []
        got = [sum(bits[8 * k + i] << i for i in range(8)) for k in range(len(bits) // 8)]
        exp_raw = M.usb_packet_bits(pid, payload)[8:]
        exp = [sum(exp_raw[8 * k + i] << i for i in range(8)) for k in range(len(exp_raw) // 8)]
        good_residue = crc_serial_msb(exp_raw[8:], 0x8005, 16, 0xFFFF)
        if corrupt is None:
            assert got == exp, ([hex(x) for x in got], [hex(x) for x in exp])
            assert crc == good_residue == 0x800D, hex(crc)
        else:
            assert crc != 0x800D, "corruption not detected by CRC"
        results.append((got, hex(crc)))
        await b.cycles(int(bit_cycles * 10))
    return {"packets": results}


# ====================================================================== 10BASE-T TX
async def eth10_tx(b):
    p = prog("eth10_tx")
    frame = M.eth_frame([0xFF] * 6, [0x02, 0x00, 0x00, 0x12, 0x34, 0x56], 0x88B5, b"Loom says hi!")
    bits = [(x >> i) & 1 for x in frame for i in range(8)]
    rec = []

    class Rec:
        def drives(self):
            return {}

        def tick(self, cyc, lv):
            rec.append((lv[16], lv[17]))

    await b.load(p, 0)
    await b.configure(0, p.config(0, clkdiv=3, out_base=16, out_count=2, set_base=16, set_count=2,
                                  code_mode=R.CODE_MANCH, manch_pol=0, diff=True, out_shift_right=True,
                                  autopull=True))
    b.add(Rec())
    words = pack_bits_lsb(bits)
    # 10 Mb/s needs a word every 192 clocks: switch the host link to quad-SPI writes
    # (all writes from here on are quad), prefill, enable, then stream paced writes.
    await b.write(R.HOSTCFG, 1)
    await b.write_quad(R.ENG(0, R.TXF), len(bits), words[0])
    await b.write_quad(R.CTRL, 0x11)
    t = b.cycle + 20                # let the engine take the prefilled count and first word
    for w in words[1:]:
        if b.cycle < t:
            await b.cycles(t - b.cycle)
        await b.write_quad(R.ENG(0, R.TXF), w)
        t += 190
    await b.write_quad(R.HOSTCFG, 0)
    await b.cycles(len(bits) * 6 + 200)
    s = [x for x, _ in rec]
    assert all(x != y for x, y in rec if x or y) or True
    i = next(k for k in range(1, len(s)) if s[k - 1] == 0 and s[k] == 1)
    dec = []
    t = i
    while t < len(s) and s[t - 1] != s[t]:
        dec.append(s[t])
        t += 6
    got = bytes(sum(dec[8 * k + j] << j for j in range(8)) for k in range(len(dec) // 8))
    assert got == frame, (len(got), len(frame), got[:16].hex(), frame[:16].hex())
    # differential pair is always complementary while transmitting
    seg = rec[i:i + len(bits) * 6]
    assert all(x != y for x, y in seg), "TD+/TD- not complementary"
    return {"bytes": len(frame)}


# ====================================================================== WS2812
async def ws2812(b):
    p = prog("ws2812")
    mon = b.add(M.Ws2812Monitor(18, cycles_per_us=60))
    await b.load(p, 0)
    await b.configure(0, p.config(0, clkdiv=7.5, sideset_base=18, autopull=True, pull_thresh=24))
    await b.enable(0b0001)
    pixels = [0xFF0000, 0x00A55A, 0x123456]
    await b.push_wait(0, *[px << 8 for px in pixels])
    await b.wait_for(lambda: len(mon.bits) >= 72, max_cycles=72 * 80)
    exp = [(px >> (23 - i)) & 1 for px in pixels for i in range(24)]
    assert mon.bits[:72] == exp
    highs = mon.highs[:72]
    zeros = [h for h, bt in zip(highs, exp) if not bt]
    ones = [h for h, bt in zip(highs, exp) if bt]
    assert all(0.25 <= h <= 0.55 for h in zeros), zeros[:4]
    assert all(0.65 <= h <= 1.0 for h in ones), ones[:4]
    return {"t0h_us": zeros[0], "t1h_us": ones[0]}


# ====================================================================== pulse capture
async def pulse_capture(b):
    p = prog("pulse_capture")
    intervals = [137, 52, 900, 33, 4000, 75, 610, 220]
    await b.load(p, 0)
    await b.configure(0, p.config(0, ts_pin=11, ts_edge=R.EDGE_BOTH, autopush=True, push_thresh=32))
    await b.enable(0b0001)
    b.add(M.PulseGen(11, intervals, start=b.cycle + 300))
    words = await b.collect(0, len(intervals) // 2, max_cycles=sum(intervals) + 4000)
    got = []
    for w in words:
        got += [w >> 16, w & 0xFFFF]
    assert got[1:] == intervals[1:], got      # first interval is measured from reset
    return {"intervals": got[1:]}


# ====================================================================== PS/2
async def ps2_rx(b):
    p = prog("ps2_rx", CLK=9)
    codes = [0x1C, 0xF0, 0x1C, 0x5A]
    dev = b.add(M.PS2Device(clk=9, data=8, half_cycles=120))
    await b.load(p, 0)
    await b.configure(0, p.config(0, in_base=8, in_shift_right=True))
    await b.enable(0b0001)
    for c in codes:
        dev.send(c)
    words = await b.collect(0, len(codes), max_cycles=len(codes) * 11 * 300 + 2000)
    frames = [w >> 21 for w in words]
    got = [(f >> 1) & 0xFF for f in frames]
    assert got == codes, [hex(x) for x in got]
    for f, c in zip(frames, codes):
        assert f & 1 == 0 and (f >> 10) & 1 == 1
        assert ((f >> 9) & 1) == 1 ^ (bin(c).count("1") & 1)
    return {"codes": got}


# ====================================================================== CAN TX
async def can_tx(b, bit_cycles=None):
    bit_cycles = bit_cycles or (120 if b.slow_host else 20)    # 500 kbit/s with the real SPI host
    p = prog("can_tx")
    frame = M.can_frame_bits(0x123, [0xDE, 0xAD, 0x00, 0x00, 0x00])
    stuffed = M.can_stuff(frame)
    smp = b.add(M.BitSampler(19, bit_cycles, len(stuffed) + 13))
    await b.load(p, 0)
    await b.configure(0, p.config(0, clkdiv=bit_cycles, out_base=19, out_count=1, set_base=19, set_count=1,
                                  stuff_en=False, stuff_mode=1, stuff_n=5, idle_level=1, autopull=True))
    await b.write(R.GPIO, 1 << 19)     # recessive idle
    await b.enable(0b0001)
    words = pack_bits_msb(frame)
    await b.push(0, len(frame), words[0])
    for w in words[1:]:
        await b.push_wait(0, w)
    await b.wait_for(lambda: len(smp.bits) >= len(stuffed) + 13, max_cycles=bit_cycles * 200)
    assert smp.bits[:len(stuffed)] == stuffed, "stuffed bitstream mismatch"
    assert smp.bits[len(stuffed):] == [1] * 13
    return {"bits": len(frame), "stuffed": len(stuffed)}


# ====================================================================== CAN RX
def _frame_words(words, msb_first):
    """Reassemble the bit list from: full words..., partial word, status word."""
    full, partial, status = words[:-2], words[-2], words[-1]
    n = status & 63
    if msb_first:
        bits = [(w >> (31 - i)) & 1 for w in full for i in range(32)]
        bits += [(partial >> (n - 1 - i)) & 1 for i in range(n)]
    else:
        bits = [(w >> i) & 1 for w in full for i in range(32)]
        bits += [((partial >> (32 - n)) >> i) & 1 for i in range(n)] if n else []
    return bits


async def _rx_frame(b, eng, settle):
    words = []
    while True:
        words += await b.drain(eng)
        if (await b.irq()) & (1 << eng):
            words += await b.drain(eng)
            await b.clear_irq(1 << eng)
            return words
        await b.cycles(settle)


async def can_rx(b, bit_cycles=120, ppm=0):
    p = prog("can_rx")
    drv = b.add(M.LevelDriver(8, bit_cycles, idle=1, ppm=ppm))
    await b.load(p, 0)
    await b.configure(0, p.config(0, clkdiv=bit_cycles, in_base=8, code_mode=R.CODE_NONE, stuff_en=False,
                                  stuff_mode=1, stuff_n=5, ts_pin=8, ts_edge=R.EDGE_FALL, dpll=True,
                                  autopush=True, trap_en=True, trap_event=R.EV_STUFF_ERR, trap_pol=1,
                                  trap_addr=p.addr("eof")))
    await b.enable(0b0001)
    frames = [(0x123, [0xDE, 0xAD, 0x00, 0x00, 0x00]), (0x7FF, [0xFF] * 8), (0x000, [])]
    got_all = []
    for ident, data in frames:
        bits = M.can_frame_bits(ident, data)
        drv.send([1] * 12 + M.can_stuff(bits) + [1] * 10)     # idle, frame, delimiters + EOF + IFS
        rx = _frame_words(await _rx_frame(b, 0, bit_cycles * 4), msb_first=True)
        assert rx[:len(bits) - 1] == bits[1:], (hex(ident), rx[:40], bits[1:41])
        # CRC check on the received bits (incl. the SOF the program consumed): residue 0
        assert M.can_crc15([0] + rx[:len(bits) - 1]) == 0
        got_all.append((hex(ident), len(bits)))
        await b.wait_for(lambda: drv.done, max_cycles=bit_cycles * 200)
    return {"frames": got_all}


# ====================================================================== Manchester RX
async def manchester_rx_phases(b, bitrate_cycles=60):
    """Regression for the DPLL edge/tick coincidence bug: frames start at every offset in a half-bit."""
    p = prog("manchester_rx")
    half = bitrate_cycles // 2
    drv = b.add(M.LevelDriver(8, half, idle=0))
    await b.load(p, 0)
    await b.configure(0, p.config(0, clkdiv=half, in_base=8, code_mode=R.CODE_MANCH, manch_pol=0,
                                  ts_pin=8, ts_edge=R.EDGE_BOTH, dpll=True, in_shift_right=True,
                                  autopush=True, trap_en=True, trap_event=R.EV_TIMEOUT, trap_pol=1,
                                  trap_addr=p.addr("eof"), timeout=int(bitrate_cycles * 1.5)))
    await b.enable(0b0001)
    payload = [0x55] * 4 + [0xD5, 0xA5, 0x3C, 0x0F]
    bits = [(x >> i) & 1 for x in payload for i in range(8)]
    for phase in range(half):
        await b.cycles(phase + 7)
        drv.send(M.manchester_levels(bits) + [0] * 4)
        rx = _frame_words(await _rx_frame(b, 0, bitrate_cycles * 4), msb_first=False)
        k = next(i for i in range(1, len(rx)) if rx[i - 1] == 1 and rx[i] == 1) + 1
        got = [sum(rx[k + 8 * j + i] << i for i in range(8)) for j in range(3)]
        assert got == [0xA5, 0x3C, 0x0F], (phase, [hex(g) for g in got])
        await b.wait_for(lambda: drv.done, max_cycles=bitrate_cycles * 100)
    return {"phases": half}


async def manchester_rx(b, bitrate_cycles=60, ppm=0):
    p = prog("manchester_rx")
    half = bitrate_cycles // 2
    drv = b.add(M.LevelDriver(8, half, idle=0, ppm=ppm))
    await b.load(p, 0)
    await b.configure(0, p.config(0, clkdiv=half, in_base=8, code_mode=R.CODE_MANCH, manch_pol=0,
                                  ts_pin=8, ts_edge=R.EDGE_BOTH, dpll=True, in_shift_right=True,
                                  autopush=True, trap_en=True, trap_event=R.EV_TIMEOUT, trap_pol=1,
                                  trap_addr=p.addr("eof"), timeout=int(bitrate_cycles * 1.5)))
    await b.enable(0b0001)
    frame = M.eth_frame([0x02, 0, 0, 0, 0, 1], [0x02, 0, 0, 0, 0, 2], 0x88B5, b"Manchester via Loom")
    bits = [(x >> i) & 1 for x in frame for i in range(8)]
    drv.send([0] * 8 + M.manchester_levels(bits) + [0] * 8)
    rx = _frame_words(await _rx_frame(b, 0, bitrate_cycles * 8), msb_first=False)
    # align on the SFD (…1 0 1 0 1 1): first occurrence of two consecutive ones
    k = next(i for i in range(1, len(rx)) if rx[i - 1] == 1 and rx[i] == 1) + 1
    body = rx[k:k + (len(frame) - 8) * 8]
    got = bytes(sum(body[8 * j + i] << i for i in range(8)) for j in range(len(body) // 8))
    assert got == frame[8:], (got[:16].hex(), frame[8:24].hex())
    return {"bytes": len(got), "ppm": ppm}


# ====================================================================== I2C target
async def i2c_target(b, half=150):
    """Loom as an I2C target at address 0x2A, driven by an independent controller model."""
    p = prog("i2c_target", SCL=1)
    b.pullups |= {0, 1}
    ctl = b.add(M.I2CController(sda=0, scl=1, half_cycles=half))
    await b.load(p, 0)
    await b.configure(0, p.config(0, in_base=0, jmp_pin=0, set_base=0, set_count=1, out_base=0, out_count=1,
                                  ts_pin=0, ts_edge=R.EDGE_BOTH, pat_mask=1 << 1, pat_val=1 << 1,
                                  trap_en=True, trap_event=R.EV_QEDGE, trap_pol=1, trap_addr=p.addr("cond")))
    await b.write(R.GPIO, 0b01)       # SDA released
    await b.write(R.OD, 0b01)         # SDA open drain (the target never drives SCL)
    await b.push(0, 0x2A)             # own address
    await b.enable(0b0001)
    await b.cycles(50)
    # write two bytes
    ctl.start(); ctl.write(0x2A << 1); ctl.write(0x5E); ctl.write(0xC3); ctl.stop()
    await b.wait_for(lambda: ctl.idle, max_cycles=half * 200)
    rx = await b.drain(0)
    assert ctl.acks == [True, True, True], ctl.acks
    assert [w & 0xFF for w in rx] == [0x5E, 0xC3], [hex(w) for w in rx]
    # a transfer to another address is ignored (no ACK, nothing received)
    ctl.acks.clear()
    ctl.start(); ctl.write(0x13 << 1); ctl.stop()
    await b.wait_for(lambda: ctl.idle, max_cycles=half * 100)
    assert ctl.acks == [False] and await b.drain(0) == []
    # read three bytes (last one NACKed) after a repeated START
    await b.push(0, 0xA5 << 24, 0x3C << 24)
    ctl.acks.clear()
    ctl.start(); ctl.write(0x2A << 1); ctl.write(0x07)            # "register" byte
    ctl.start(); ctl.write((0x2A << 1) | 1)
    ctl.read_byte(); ctl.read_byte(); ctl.read_byte(ack=False); ctl.stop()
    await b.wait_for(lambda: ctl.idle, max_cycles=half * 400)
    assert ctl.acks == [True, True, True], ctl.acks
    assert ctl.read == [0xA5, 0x3C, 0xFF], [hex(x) for x in ctl.read]   # FIFO ran dry -> 0xFF
    assert [w & 0xFF for w in await b.drain(0)] == [0x07]
    return {"written": [0x5E, 0xC3], "read": ctl.read}


# ====================================================================== everything at once
async def concurrent(b):
    """UART TX + UART RX + SPI controller + pulse capture on four engines simultaneously."""
    bc = 120 if b.slow_host else 32
    tx_mon = b.add(M.UartMonitor(13, bc))
    drv = b.add(M.UartDriver(8, bc, gap=2))
    tgt = b.add(M.SpiTarget(sck=14, mosi=15, miso=9, responses=[0x11, 0x22, 0x33]))
    ptx, prx, pspi, pcap = prog("uart_tx"), prog("uart_rx"), prog("spi_cpha0"), prog("pulse_capture")
    await b.load(ptx, 0)
    await b.load(prx, 4, engine=1)
    await b.load(pspi, 13, engine=2)
    await b.load(pcap, 15, engine=3)
    await b.configure(0, ptx.config(0, clkdiv=bc / 4, out_base=13, out_count=1, sideset_base=13,
                                    out_shift_right=True))
    await b.configure(1, prx.config(4, clkdiv=bc / 8, in_base=8, jmp_pin=8, in_shift_right=True))
    await b.configure(2, pspi.config(13, clkdiv=3, sideset_base=14, out_base=15, out_count=1, in_base=9,
                                     autopull=True, autopush=True, pull_thresh=8, push_thresh=8))
    await b.configure(3, pcap.config(15, ts_pin=11, ts_edge=R.EDGE_BOTH, autopush=True, push_thresh=32))
    await b.write(R.GPIO, 1 << 13)
    await b.enable(0b1111)
    b.add(M.PulseGen(11, [300, 120, 800, 77], start=b.cycle + 200))
    await b.push(0, 0x4C, 0x4F)
    await b.push(2, 0xAA << 24, 0x55 << 24)
    for d in (0x10, 0x20):
        drv.send(d)
    rx_u = await b.collect(1, 2, max_cycles=bc * 40)
    rx_s = await b.collect(2, 2, max_cycles=20000)
    rx_c = await b.collect(3, 2, max_cycles=20000)
    await b.wait_for(lambda: len(tx_mon.bytes) >= 2, max_cycles=bc * 30)
    assert tx_mon.bytes[:2] == [0x4C, 0x4F]
    assert [w >> 24 for w in rx_u] == [0x10, 0x20]
    assert tgt.received[:2] == [0xAA, 0x55] and [w & 0xFF for w in rx_s] == [0x11, 0x22]
    got = [rx_c[0] & 0xFFFF, rx_c[1] >> 16, rx_c[1] & 0xFFFF]
    assert got == [120, 800, 77], got
    return {"ok": True}


ALL = [
    ("uart_loopback", uart_loopback, {}),
    ("spi_mode0", spi_controller, {"mode": 0}),
    ("spi_mode1", spi_controller, {"mode": 1}),
    ("spi_mode2", spi_controller, {"mode": 2}),
    ("spi_mode3", spi_controller, {"mode": 3}),
    ("spi_target", spi_target, {}),
    ("i2c", i2c_controller, {}),
    ("i2c_stretch", i2c_controller, {"stretch": 37}),
    ("i2c_target", i2c_target, {}),
    ("usb_ls_tx", usb_ls_tx, {}),
    ("usb_ls_rx", usb_ls_rx, {}),
    ("usb_ls_rx_skew", usb_ls_rx, {"ppm": 15000}),
    ("usb_ls_tx_40mhz", usb_ls_tx, {"bit_cycles": 40e6 / 1.5e6}),       # fractional divider 26.67
    ("usb_ls_rx_40mhz_skew", usb_ls_rx, {"bit_cycles": 40e6 / 1.5e6, "ppm": -15000}),
    ("eth10_tx", eth10_tx, {}),
    ("ws2812", ws2812, {}),
    ("pulse_capture", pulse_capture, {}),
    ("ps2_rx", ps2_rx, {}),
    ("can_tx", can_tx, {}),
    ("can_rx", can_rx, {}),
    ("can_rx_skew", can_rx, {"ppm": 8000}),
    ("manchester_rx", manchester_rx, {}),
    ("manchester_rx_skew", manchester_rx, {"ppm": -10000}),
    ("manchester_rx_phases", manchester_rx_phases, {}),
    ("concurrent", concurrent, {}),
]
