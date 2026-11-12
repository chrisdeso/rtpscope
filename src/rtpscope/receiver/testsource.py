# testsource.py --> the test pattern as aiortc tracks, so the receiver can be tested with no browser
#
# same pattern as call.js (pattern.py has the layout). both tracks read one shared clock, like a
# browser's camera and mic would, so a/v sync measured at the receiver should come out near 0.

import asyncio
import fractions
import time

import numpy as np
from aiortc import MediaStreamTrack
from av import AudioFrame, VideoFrame

from . import pattern

RATE = 48000


class SenderClock:
    """unix time from a monotonic base --> both tracks agree to the microsecond"""

    def __init__(self):
        self.mono0 = time.monotonic()
        self.unix0 = time.time()

    def now(self):
        return self.unix0 + time.monotonic() - self.mono0

    async def sleep_until(self, unix):
        await asyncio.sleep(max(0.0, unix - self.now()))


class PatternVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, clock, width=640, height=360, fps=30):
        super().__init__()
        self.clock = clock
        self.width, self.height, self.fps = width, height, fps
        self.n = 0
        self.start = None

    async def recv(self):
        if self.start is None:
            self.start = self.clock.now()
        t = self.start + self.n / self.fps
        self.n += 1
        await self.clock.sleep_until(t)
        img = np.full((self.height, self.width, 3), 60, dtype=np.uint8)
        # something moving, so the encoder has real work
        x = (self.n * 8) % self.width
        img[self.height // 2 :, x : x + 16] = 220
        pattern.draw(img, t * 1000)
        frame = VideoFrame.from_ndarray(img, format="bgr24")
        frame.pts = round((t - self.start) * 90000)
        frame.time_base = fractions.Fraction(1, 90000)
        return frame


class BeepAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, clock, ptime=0.020):
        super().__init__()
        self.clock = clock
        self.n = round(ptime * RATE)
        self.sample = 0
        self.start = None

    async def recv(self):
        if self.start is None:
            self.start = self.clock.now()
        t0 = self.start + self.sample / RATE
        await self.clock.sleep_until(t0)
        t = t0 + np.arange(self.n) / RATE
        on = (t * 1000) % 1000 < pattern.BEEP_MS
        wave = np.where(on, 0.5 * np.sin(2 * np.pi * pattern.BEEP_HZ * t), 0.0)
        data = (wave * 32767).astype(np.int16).reshape(1, -1)
        frame = AudioFrame.from_ndarray(data, format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self.sample
        frame.time_base = fractions.Fraction(1, RATE)
        self.sample += self.n
        return frame
