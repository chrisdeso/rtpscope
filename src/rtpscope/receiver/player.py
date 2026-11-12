# player.py --> decoded frames in, scheduled playout out, one PeerPlayout per person in the call
#
#   track.recv() --> media time (sync.py) --> DelayEstimator + queue (buffer.py)
#   video: tick() every few ms shows the newest due frame
#   audio: the sound card (or a fake one) pulls samples for the moment they'll actually be heard
#
# while showing/playing we also read the test pattern (pattern.py): barcode --> end to end delay,
# flash vs beep --> measured a/v sync at the output.

import asyncio
import contextlib
import csv
import logging
import time
from dataclasses import dataclass

import numpy as np
from aiortc.mediastreams import MediaStreamError

from . import pattern
from .buffer import DelayEstimator, FrameQueue, PlayoutClock, SampleQueue
from .sync import SenderClock

log = logging.getLogger(__name__)

RATE = 48000  # opus always decodes to 48 kHz
CHANNELS = 2

# monotonic --> unix. scheduling uses monotonic (never jumps), reports use unix
_UNIX_OFFSET = time.time() - time.monotonic()


def to_unix(mono):
    return mono + _UNIX_OFFSET


@dataclass
class PlayoutConfig:
    min_delay: float = 0.020
    max_delay: float = 1.0
    margin: float = 0.010
    percentile: float = 0.95
    window: float = 10.0
    decay: float = 0.01

    def estimator(self):
        return DelayEstimator(
            window=self.window,
            percentile=self.percentile,
            margin=self.margin,
            min_delay=self.min_delay,
            max_delay=self.max_delay,
        )


class Stream:
    def __init__(self, kind, clock, config):
        self.kind = kind
        self.clock = clock
        self.estimator = config.estimator()
        # used while this stream can't be synced to the other one yet
        self.local = PlayoutClock(config.decay)
        self.decay = config.decay
        self.synced = False
        self.frames = 0
        self.queue = FrameQueue() if kind == "video" else SampleQueue(RATE, CHANNELS)

    # media time switched from "since first frame" to sender clock --> old numbers are meaningless
    def resync(self, synced):
        self.synced = synced
        self.estimator.reset()
        self.queue.clear()
        self.local = PlayoutClock(self.decay)


def audio_samples(frame):
    """av.AudioFrame --> float32 (n, CHANNELS)"""
    data = frame.to_ndarray()
    channels = len(frame.layout.channels)
    data = data.reshape(channels, -1).T if frame.format.is_planar else data.reshape(-1, channels)
    if np.issubdtype(data.dtype, np.integer):
        data = data.astype(np.float32) / 32768
    if channels == 1:
        data = np.repeat(data, CHANNELS, axis=1)
    return np.ascontiguousarray(data[:, :CHANNELS], dtype=np.float32)


class PeerPlayout:
    def __init__(self, peer_id, name, config=None, video_sink=None):
        self.id = peer_id
        self.name = name
        self.config = config or PlayoutConfig()
        self.video_sink = video_sink
        self.shared = PlayoutClock(self.config.decay)
        self.streams: dict[str, Stream] = {}
        self._tasks: list[asyncio.Task] = []
        # test pattern readings
        self.flash_edge = pattern.Edge()
        self.beeps = pattern.BeepDetector(RATE)
        self.flash_times: list[float] = []
        self.beep_times: list[float] = []
        self.av_measured: list[float] = []  # flash - beep, seconds, + means video is behind
        self.e2e: list[float] = []  # display time - barcode clock, seconds
        self.video_lag: list[float] = []  # how late frames hit the screen vs their schedule
        self.last_rows: list[dict] = []
        # the sound card asks for audio this far ahead of hearing it --> audio needs that much
        # more buffer than its jitter alone says, or every chunk shows up after it was wanted
        self.audio_lead = 0.0

    # ---- input

    def attach(self, track, clock=None):
        stream = Stream(track.kind, clock or SenderClock(), self.config)
        self.streams[track.kind] = stream
        self._tasks.append(asyncio.ensure_future(self._consume(stream, track)))

    async def _consume(self, stream, track):
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            now = time.monotonic()
            if frame.pts is None or frame.time_base is None:
                continue
            media_t, synced = stream.clock.media_time(frame.pts, frame.time_base)
            if synced != stream.synced:
                log.info("%s %s: %s", self.name, stream.kind, "synced" if synced else "unsynced")
                stream.resync(synced)
            stream.frames += 1
            stream.estimator.add(media_t, now)
            self._update_clocks(now)
            play_t = self.clock_of(stream).play_time(now)
            if stream.kind == "video":
                stream.queue.push(media_t, frame, play_t)
            else:
                stream.queue.push(round(media_t * RATE), audio_samples(frame))

    @property
    def synced(self):
        return bool(self.streams) and all(s.synced for s in self.streams.values())

    def clock_of(self, stream):
        return self.shared if self.synced else stream.local

    def _update_clocks(self, now):
        offsets = []
        for stream in self.streams.values():
            desired = stream.estimator.desired_offset()
            if desired is not None:
                if stream.kind == "audio":
                    desired += self.audio_lead
                stream.local.update(desired, now)
            offsets.append(desired)
        # lip sync: both streams play on the clock of whichever needs more delay
        if self.synced and None not in offsets:
            self.shared.update(max(offsets), now)

    # ---- output

    def tick(self, now):
        """show the video frame that's due, if any"""
        stream = self.streams.get("video")
        if stream is None:
            return
        play_t = self.clock_of(stream).play_time(now)
        if play_t is None:
            return
        assert isinstance(stream.queue, FrameQueue)
        due = stream.queue.pop_due(play_t)
        if due is None:
            return
        media_t, frame = due
        self.video_lag.append(play_t - media_t)
        self._read_pattern(frame, now)
        if self.video_sink is not None:
            self.video_sink.show(self, frame)

    def _read_pattern(self, frame, now):
        gray = frame.to_ndarray(format="gray")
        shown = to_unix(now)
        sent_ms = pattern.read_barcode(gray)
        if sent_ms is not None:
            self.e2e.append(shown - sent_ms / 1000)
        if self.flash_edge.feed(pattern.read_flash(gray), shown):
            self.flash_times.append(shown)
            self._pair()

    def read_audio(self, heard_at, n):
        """n samples whose first one will be heard at monotonic time `heard_at`, or None"""
        stream = self.streams.get("audio")
        if stream is None:
            return None
        lead = max(0.0, heard_at - time.monotonic())
        self.audio_lead = max(lead, self.audio_lead + (lead - self.audio_lead) * 0.01)
        play_t = self.clock_of(stream).play_time(heard_at)
        if play_t is None:
            return None
        assert isinstance(stream.queue, SampleQueue)
        out = stream.queue.read(round(play_t * RATE), n)
        for onset in self.beeps.feed(out.mean(axis=1), to_unix(heard_at)):
            self.beep_times.append(onset)
            self._pair()
        return out

    # match each flash with the beep from the same second
    def _pair(self):
        if not self.flash_times or not self.beep_times:
            return
        flash, beep = self.flash_times[-1], self.beep_times[-1]
        if abs(flash - beep) < 0.5:
            self.av_measured.append(flash - beep)
            self.flash_times.clear()
            self.beep_times.clear()

    # ---- stats

    def snapshot(self):
        """one row per stream for the last interval, then reset the interval's lists"""
        now = time.monotonic()
        rows = []

        def mean_ms(values):
            return round(1000 * float(np.mean(values)), 1) if values else None

        av, e2e, lag = mean_ms(self.av_measured), mean_ms(self.e2e), mean_ms(self.video_lag)
        self.av_measured, self.e2e, self.video_lag = [], [], []
        for kind, s in sorted(self.streams.items()):
            play_t = self.clock_of(s).play_time(now)
            if isinstance(s.queue, FrameQueue):
                depth = s.queue.depth(play_t) if play_t is not None else 0.0
                dropped, concealed_ms = s.queue.dropped, None
            else:
                depth = s.queue.depth()
                dropped, concealed_ms = None, round(1000 * s.queue.concealed / RATE)
            rows.append(
                {
                    "time": round(to_unix(now), 3),
                    "peer": self.name,
                    "kind": kind,
                    "synced": int(self.synced),
                    "frames": s.frames,
                    "late": s.queue.late,
                    "dropped": dropped,
                    "concealed_ms": concealed_ms,
                    "buffer_ms": round(1000 * depth, 1),
                    "target_ms": round(1000 * s.estimator.target, 1),
                    "jitter_ms": round(1000 * s.estimator.jitter, 1),
                    "video_lag_ms": lag if kind == "video" else None,
                    "av_sync_ms": av,
                    "e2e_ms": e2e if kind == "video" else None,
                }
            )
        # the video window's overlay shows these
        self.last_rows = rows
        return rows

    async def close(self):
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


FIELDS = [
    "time",
    "peer",
    "kind",
    "synced",
    "frames",
    "late",
    "dropped",
    "concealed_ms",
    "buffer_ms",
    "target_ms",
    "jitter_ms",
    "video_lag_ms",
    "av_sync_ms",
    "e2e_ms",
]


class Receiver:
    """everyone we're receiving from, the output devices and the stats log"""

    def __init__(self, config=None, video_sink=None, audio_sink=None, csv_path=None, tick=0.004):
        self.config = config or PlayoutConfig()
        self.video_sink = video_sink
        self.audio_sink = audio_sink
        self.peers: dict[str, PeerPlayout] = {}
        self.tick_interval = tick
        self.history: list[dict] = []
        self._csv_file = open(csv_path, "w", newline="") if csv_path else None  # noqa: SIM115
        self._csv = csv.DictWriter(self._csv_file, FIELDS) if self._csv_file else None
        if self._csv:
            self._csv.writeheader()

    def peer(self, peer_id, name):
        if peer_id not in self.peers:
            self.peers[peer_id] = PeerPlayout(peer_id, name, self.config, self.video_sink)
        return self.peers[peer_id]

    async def remove(self, peer_id):
        peer = self.peers.pop(peer_id, None)
        if peer is not None:
            await peer.close()
            if self.video_sink is not None:
                self.video_sink.close(peer)

    # sound card callback --> mix everyone
    def mix(self, heard_at, n):
        out = np.zeros((n, CHANNELS), dtype=np.float32)
        for peer in list(self.peers.values()):
            samples = peer.read_audio(heard_at, n)
            if samples is not None:
                out += samples
        np.clip(out, -1, 1, out=out)
        return out

    async def run(self, stats_every=1.0, duration=None):
        if self.audio_sink is not None:
            self.audio_sink.start(self.mix)
        started = last_stats = time.monotonic()
        try:
            while duration is None or time.monotonic() - started < duration:
                now = time.monotonic()
                for peer in list(self.peers.values()):
                    peer.tick(now)
                if self.video_sink is not None and not self.video_sink.pump():
                    break  # window closed / q pressed
                if now - last_stats >= stats_every:
                    last_stats = now
                    self.log_stats()
                await asyncio.sleep(self.tick_interval)
        finally:
            await self.close()

    def log_stats(self):
        for peer in self.peers.values():
            for row in peer.snapshot():
                self.history.append(row)
                if self._csv:
                    self._csv.writerow(row)
                log.info(
                    "%-10s %s %s frames=%-5d late=%-3d buf=%5.1fms target=%5.1fms jit=%5.1fms"
                    " a/v=%s e2e=%s",
                    row["peer"],
                    row["kind"],
                    "sync" if row["synced"] else "----",
                    row["frames"],
                    row["late"],
                    row["buffer_ms"],
                    row["target_ms"],
                    row["jitter_ms"],
                    _ms(row["av_sync_ms"]),
                    _ms(row["e2e_ms"]),
                )
        if self._csv_file:
            self._csv_file.flush()

    async def close(self):
        if self.audio_sink is not None:
            self.audio_sink.stop()
        for peer_id in list(self.peers):
            await self.remove(peer_id)
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None
            self._csv = None


def _ms(value):
    return "-" if value is None else f"{value:+.0f}ms"
