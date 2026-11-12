# pattern.py --> the measured-run test pattern, shared with startPattern() in call/static/call.js
#
# what the sender draws on every frame:
#   - a barcode across the top: the sender's clock (ms since epoch) when the frame was drawn
#   - a flash square (top right) that goes white for FLASH_MS at the start of every second
# and on its audio track:
#   - a BEEP_HZ beep for BEEP_MS at the start of every second
#
# barcode delay = when the frame shows up here minus the clock in the barcode (end to end delay,
# needs the two clocks synced, or the same machine). flash vs beep = audio/video sync.
#
# everything is in fractions of the frame so it still reads after the ladder scales it down.
# change a number here --> change it in call.js too.

import numpy as np

# barcode: 2 guard cells (white, black) + 42 data bits + 8 crc bits, one column each
GUARD = (1, 0)
DATA_BITS = 42  # ms since epoch, good until 2109
CRC_BITS = 8
CELLS = len(GUARD) + DATA_BITS + CRC_BITS
BARCODE_Y = (0.0, 0.06)  # top and bottom of the strip
# flash square --> x0, y0, x1, y1
FLASH_BOX = (0.86, 0.10, 0.98, 0.30)
FLASH_MS = 100
BEEP_HZ = 1000
BEEP_MS = 100


# crc-8 (poly 0x07) over a list of bits, msb first. same loop in call.js
def crc8(bits):
    crc = 0
    for bit in bits:
        feedback = ((crc >> 7) & 1) ^ bit
        crc = (crc << 1) & 0xFF
        if feedback:
            crc ^= 0x07
    return crc


def to_bits(value, n):
    return [(value >> (n - 1 - i)) & 1 for i in range(n)]


def from_bits(bits):
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def barcode_bits(ms):
    data = to_bits(int(ms) % (1 << DATA_BITS), DATA_BITS)
    return [*GUARD, *data, *to_bits(crc8(data), CRC_BITS)]


# column edges for cell i --> same rounding as call.js
def _cell_x(i, w):
    return round(i * w / CELLS), round((i + 1) * w / CELLS)


def _box(img, box):
    h, w = img.shape[:2]
    x0, y0, x1, y1 = box
    return round(y0 * h), round(y1 * h), round(x0 * w), round(x1 * w)


def flash_on(ms):
    return int(ms) % 1000 < FLASH_MS


def draw(img, ms):
    """draw barcode + flash square for sender clock `ms` onto an 8-bit image (gray or bgr)"""
    h, w = img.shape[:2]
    y0, y1 = round(BARCODE_Y[0] * h), round(BARCODE_Y[1] * h)
    for i, bit in enumerate(barcode_bits(ms)):
        x0, x1 = _cell_x(i, w)
        img[y0:y1, x0:x1] = 255 if bit else 0
    fy0, fy1, fx0, fx1 = _box(img, FLASH_BOX)
    img[fy0:fy1, fx0:fx1] = 255 if flash_on(ms) else 0
    return img


def read_barcode(gray):
    """sender clock in ms from a frame's luma plane, or None if there's no (readable) barcode"""
    h, w = gray.shape[:2]
    if h < 40 or w < CELLS * 4:
        return None
    # middle rows of the strip --> skip the edges, the codec smears them into the bars below
    top, bottom = BARCODE_Y
    span = bottom - top
    r0 = int((top + span * 0.2) * h)
    r1 = max(int((top + span * 0.8) * h), r0 + 1)
    columns = gray[r0:r1].astype(np.float32).mean(axis=0)
    values = []
    for i in range(CELLS):
        x0, x1 = _cell_x(i, w)
        # middle half of each cell, same reason
        a, b = x0 + (x1 - x0) // 4, x1 - (x1 - x0) // 4
        values.append(float(columns[a : max(b, a + 1)].mean()))
    white, black = values[0], values[1]
    if white - black < 64:
        return None
    threshold = (white + black) / 2
    bits = [int(v > threshold) for v in values]
    data = bits[len(GUARD) : len(GUARD) + DATA_BITS]
    if crc8(data) != from_bits(bits[len(GUARD) + DATA_BITS :]):
        return None
    return from_bits(data)


def read_flash(gray):
    """True when the flash square is lit"""
    y0, y1, x0, x1 = _box(gray, FLASH_BOX)
    # inner 60% --> ignore ringing at the square's edges
    dy, dx = (y1 - y0) // 5, (x1 - x0) // 5
    patch = gray[y0 + dy : y1 - dy, x0 + dx : x1 - dx]
    return patch.size > 0 and float(patch.mean()) > 128


def tone_ratio(block, rate, freq=BEEP_HZ):
    """share of a mono block's power that sits at `freq` (goertzel), 0..1"""
    n = len(block)
    if n == 0:
        return 0.0
    total = float(np.dot(block, block))
    if total < 1e-9:
        return 0.0
    w = 2 * np.pi * freq / rate
    t = np.arange(n)
    re = float(np.dot(block, np.cos(w * t)))
    im = float(np.dot(block, np.sin(w * t)))
    # a pure tone puts (n/2) * amplitude^2 ... normalise so a clean tone reads ~1
    return min(1.0, 2 * (re * re + im * im) / (n * total))


class Edge:
    """rising edge detector with a hold-off --> one event per flash / beep"""

    def __init__(self, holdoff=0.5):
        self.holdoff = holdoff
        self.on = False
        self.last = None

    def feed(self, on, t):
        rising = on and not self.on
        self.on = on
        if rising and (self.last is None or t - self.last >= self.holdoff):
            self.last = t
            return True
        return False


class BeepDetector:
    """finds beep onsets in played-out audio, in 10 ms blocks, sample accurate inside the block"""

    def __init__(self, rate, block_ms=10, threshold=0.5):
        self.rate = rate
        self.block = rate * block_ms // 1000
        self.threshold = threshold
        self.edge = Edge()
        self._pending = np.zeros(0, dtype=np.float32)
        self._pending_t = None

    def feed(self, mono, t0):
        """mono float samples whose first sample plays at time t0 --> list of onset times"""
        if self._pending_t is None or len(self._pending) == 0:
            self._pending_t = t0 - len(self._pending) / self.rate
        self._pending = np.concatenate([self._pending, mono.astype(np.float32)])
        onsets = []
        while len(self._pending) >= self.block:
            block = self._pending[: self.block]
            start = self._pending_t
            self._pending = self._pending[self.block :]
            self._pending_t = start + self.block / self.rate
            on = tone_ratio(block, self.rate) > self.threshold
            if self.edge.feed(on, start):
                # first sample above half the block's peak --> onset inside the block
                peak = float(np.abs(block).max())
                idx = int(np.argmax(np.abs(block) > peak / 2))
                onsets.append(start + idx / self.rate)
                self.edge.last = onsets[-1]
        return onsets
