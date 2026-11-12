# outputs.py --> where played frames go: an opencv window and the sound card, or nowhere
#
# opencv and sounddevice are only imported when used --> headless runs and ci don't need them
# (pip install -e '.[play]' for the real ones)

import asyncio
import contextlib
import logging
import time

log = logging.getLogger(__name__)

RATE = 48000
CHANNELS = 2


class WindowSink:
    """one opencv window per person, with the buffer/sync numbers drawn on top"""

    def __init__(self, overlay=True):
        import cv2

        self.cv2 = cv2
        self.overlay = overlay
        self.windows: set[str] = set()

    def _title(self, peer):
        return f"rtpscope rx - {peer.name} ({peer.id})"

    def show(self, peer, frame):
        cv2 = self.cv2
        image = frame.to_ndarray(format="bgr24")
        if self.overlay:
            font, scale = cv2.FONT_HERSHEY_SIMPLEX, 0.5
            # bottom line first. black box behind each line (an outline via a thicker putText
            # spaces the letters differently and leaves a ghost)
            for i, line in enumerate(reversed(hud_lines(peer))):
                (w, h), base = cv2.getTextSize(line, font, scale, 1)
                y = image.shape[0] - 10 - 22 * i
                cv2.rectangle(image, (6, y - h - 5), (18 + w, y + base), (0, 0, 0), -1)
                cv2.putText(image, line, (12, y), font, scale, (255, 255, 255), 1, cv2.LINE_AA)
        title = self._title(peer)
        cv2.imshow(title, image)
        self.windows.add(title)

    def pump(self):
        """let opencv draw. False once the user closes a window or presses q/esc"""
        key = self.cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            return False
        for title in list(self.windows):
            with contextlib.suppress(self.cv2.error):
                if self.cv2.getWindowProperty(title, self.cv2.WND_PROP_VISIBLE) < 1:
                    return False
        return True

    def close(self, peer):
        title = self._title(peer)
        if title in self.windows:
            self.windows.discard(title)
            with contextlib.suppress(self.cv2.error):
                self.cv2.destroyWindow(title)


def hud_lines(peer):
    rows = {row["kind"]: row for row in peer.last_rows}
    lines = []
    video, audio = rows.get("video"), rows.get("audio")
    if video:
        lines.append(
            f"video  buf {video['buffer_ms']:.0f}ms  target {video['target_ms']:.0f}ms"
            f"  jit {video['jitter_ms']:.0f}ms  late {video['late']}  dropped {video['dropped']}"
        )
    if audio:
        lines.append(
            f"audio  buf {audio['buffer_ms']:.0f}ms  target {audio['target_ms']:.0f}ms"
            f"  jit {audio['jitter_ms']:.0f}ms  late {audio['late']}"
            f"  concealed {audio['concealed_ms']}ms"
        )
    any_row = video or audio
    if any_row:
        sync = "synced" if any_row["synced"] else "not synced yet"
        av = any_row["av_sync_ms"]
        e2e = video["e2e_ms"] if video else None
        lines.append(
            f"{sync}  a/v {'-' if av is None else f'{av:+.0f}ms'}"
            f"  e2e {'-' if e2e is None else f'{e2e:.0f}ms'}"
        )
    return lines


class NullVideoSink:
    def show(self, peer, frame):
        pass

    def pump(self):
        return True

    def close(self, peer):
        pass


class SpeakerSink:
    """sound card output. the callback asks the mixer for exactly the samples that will be heard
    when this buffer reaches the speaker"""

    def __init__(self, device=None, blocksize=480):
        import sounddevice

        self.sd = sounddevice
        self.device = device
        self.blocksize = blocksize
        self.stream = None

    def start(self, mix):
        def callback(outdata, frames, time_info, status):
            # how far ahead of "now" this buffer will be heard. some drivers report 0 here,
            # fall back to the stream's advertised latency
            ahead = time_info.outputBufferDacTime - time_info.currentTime
            if not 0 < ahead < 1:
                latency = self.stream.latency if self.stream is not None else 0.0
                ahead = latency if isinstance(latency, float) else 0.0
            outdata[:] = mix(time.monotonic() + ahead, frames)

        self.stream = self.sd.OutputStream(
            samplerate=RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=self.blocksize,
            latency="low",
            device=self.device,
            callback=callback,
        )
        self.stream.start()
        log.info("audio out: %s, latency %.0f ms", self.stream.device, 1000 * self.stream.latency)

    def stop(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None


class NullAudioSink:
    """no sound card: pull audio on the event loop at the rate a sound card would, so buffering,
    sync and beep timing all still run (and can be tested)"""

    def __init__(self, interval=0.010):
        self.interval = interval
        self.task = None

    def start(self, mix):
        self.task = asyncio.ensure_future(self._run(mix))

    async def _run(self, mix):
        last = time.monotonic()
        owed = 0.0
        while True:
            await asyncio.sleep(self.interval)
            now = time.monotonic()
            owed += (now - last) * RATE
            last = now
            n = int(owed)
            owed -= n
            if n:
                # these samples cover [now - n/RATE, now)
                mix(now - n / RATE, n)

    def stop(self):
        if self.task is not None:
            self.task.cancel()
            self.task = None
