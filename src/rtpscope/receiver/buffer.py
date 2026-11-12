# buffer.py --> our jitter buffer: decides *when* each decoded frame gets played
#
# aiortc already has a "JitterBuffer", but it only puts packets back in order and glues them into
# frames. it hands each frame on the moment it's complete, so playing frames as they come out of
# aiortc plays the network's jitter straight to the screen/speaker.
#
# here every frame has a media time (its rtp timestamp in seconds, see sync.py) and an arrival time
# (when the decoder handed it to us). transit = arrival - media time. transit includes the clock
# difference between the two machines, which we don't know, but it's the same for every frame, so
# only the *spread* of transit matters:
#
#   base   = smallest transit in the last `window` seconds (the luckiest frame)
#   target = 95th percentile of (transit - base) + margin   --> how much wobble to absorb
#   offset = base + target       --> media time t plays at our time t + offset
#
# a frame that arrives after its play time is late. raising the offset makes fewer frames late but
# adds delay to everything --> the jitter buffer tradeoff the experiments measure.

import bisect
import threading
from collections import deque

import numpy as np


class DelayEstimator:
    """how much playout delay one stream needs, from its frames' arrival wobble"""

    def __init__(self, window=10.0, percentile=0.95, margin=0.010, min_delay=0.020, max_delay=1.0):
        self.window = window
        self.percentile = percentile
        self.margin = margin
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.reset()

    def reset(self):
        # (arrival, transit) for the last `window` seconds
        self._samples = deque()
        self._last_transit = None
        # rfc 3550 interarrival jitter, seconds --> same smoothing as rtcp receiver reports
        self.jitter = 0.0

    def add(self, media_t, arrival):
        transit = arrival - media_t
        if self._last_transit is not None:
            self.jitter += (abs(transit - self._last_transit) - self.jitter) / 16
        self._last_transit = transit
        self._samples.append((arrival, transit))
        while self._samples[0][0] < arrival - self.window:
            self._samples.popleft()

    @property
    def base(self):
        return min(t for _, t in self._samples) if self._samples else None

    @property
    def target(self):
        """delay above the luckiest frame, seconds"""
        if not self._samples:
            return self.min_delay
        transits = np.fromiter((t for _, t in self._samples), dtype=np.float64)
        spread = float(np.quantile(transits - transits.min(), self.percentile))
        return min(max(spread + self.margin, self.min_delay), self.max_delay)

    def desired_offset(self):
        base = self.base
        return None if base is None else base + self.target


class PlayoutClock:
    """wall time --> the media time that should be playing right now (play_t = now - offset)

    more delay is taken at once (a late frame is worse than a few extra ms), less delay is given
    back slowly, `decay` seconds per second, so playback speeds up a little instead of jumping.
    1% is slow enough for SampleQueue to absorb by skipping samples, no audible jump."""

    def __init__(self, decay=0.01):
        self.decay = decay
        self.offset = None
        self._last = None

    def update(self, desired, now):
        if self.offset is None or desired > self.offset:
            self.offset = desired
        elif self._last is not None:
            self.offset = max(desired, self.offset - self.decay * (now - self._last))
        self._last = now

    def play_time(self, now):
        return None if self.offset is None else now - self.offset


class FrameQueue:
    """video frames waiting for their play time, oldest first"""

    def __init__(self):
        self.clear()
        self.late = 0  # arrived after their play time (may still be shown, just late)
        self.dropped = 0  # never shown: older than something already on screen

    def clear(self):
        self._times = []
        self._items = []
        self.last_shown = None

    def __len__(self):
        return len(self._items)

    def push(self, media_t, item, play_t=None):
        if play_t is not None and media_t < play_t:
            self.late += 1
        if self.last_shown is not None and media_t <= self.last_shown:
            self.dropped += 1
            return False
        i = bisect.bisect(self._times, media_t)
        self._times.insert(i, media_t)
        self._items.insert(i, item)
        return True

    def pop_due(self, play_t):
        """newest frame that's due, or None. older due frames are skipped (counted as dropped)"""
        n = bisect.bisect(self._times, play_t)
        if n == 0:
            return None
        self.dropped += n - 1
        media_t, item = self._times[n - 1], self._items[n - 1]
        del self._times[:n], self._items[:n]
        self.last_shown = media_t
        return media_t, item

    def depth(self, play_t):
        """seconds of video buffered ahead of what's playing"""
        return max(0.0, self._times[-1] - play_t) if self._times else 0.0


class SampleQueue:
    """audio samples placed by media time, read out at sample positions. thread safe: the sound
    card's callback reads from its own thread"""

    def __init__(self, rate, channels=2, jump=0.020, nudge=0.002):
        self.rate = rate
        self.channels = channels
        # how far the read cursor may drift from where the clock says it should be:
        # past `jump` --> jump there (one glitch)
        # past `nudge` --> skip/repeat a few samples per read until it's back
        self.jump = round(jump * rate)
        self.nudge = round(nudge * rate)
        self._lock = threading.Lock()
        self.late = 0  # chunks that came after their samples were already played
        self.concealed = 0  # samples played as silence because nothing was there
        self.clear()

    def clear(self):
        with self._lock:
            self._starts = []
            self._chunks = []
            self.cursor = None
            self._started = False

    def push(self, start, samples):
        """samples: float32 (n, channels), first sample at position `start`"""
        with self._lock:
            end = start + len(samples)
            if self.cursor is not None:
                if end <= self.cursor:
                    self.late += 1
                    return False
                if start < self.cursor:
                    samples = samples[self.cursor - start :]
                    start = self.cursor
            i = bisect.bisect(self._starts, start)
            self._starts.insert(i, start)
            self._chunks.insert(i, samples)
            self._started = True
            return True

    def read(self, ideal, n):
        """n samples for playback. `ideal` is where the playout clock says we should be"""
        with self._lock:
            if self.cursor is None:
                self.cursor = ideal
            error = ideal - self.cursor
            if abs(error) > self.jump:
                self.cursor = ideal
            elif abs(error) > self.nudge:
                # close the gap at most 2% per read --> drift fix without an audible jump
                self.cursor += int(np.sign(error)) * min(abs(error), max(1, n // 50))
            start, end = self.cursor, self.cursor + n
            out = np.zeros((n, self.channels), dtype=np.float32)
            filled = 0
            keep = 0
            for i, (s, chunk) in enumerate(zip(self._starts, self._chunks, strict=True)):
                e = s + len(chunk)
                if e <= start:
                    keep = i + 1  # fully played --> drop it
                    continue
                if s >= end:
                    break
                a, b = max(s, start), min(e, end)
                out[a - start : b - start] = chunk[a - s : b - s, : self.channels]
                filled += b - a
            del self._starts[:keep], self._chunks[:keep]
            # only count silence once audio has actually started
            if self._started:
                self.concealed += n - filled
            self.cursor = end
            return out

    def depth(self):
        """seconds of audio buffered ahead of the read cursor"""
        with self._lock:
            if not self._chunks or self.cursor is None:
                return 0.0
            end = max(s + len(c) for s, c in zip(self._starts, self._chunks, strict=True))
            return max(0, end - self.cursor) / self.rate
