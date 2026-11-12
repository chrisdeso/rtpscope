# python -m rtpscope.receiver --room lab  --> join a call and play it with our own buffering + sync
import argparse
import asyncio
import contextlib
import logging
import sys

from .client import CallClient
from .player import PlayoutConfig, Receiver


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m rtpscope.receiver",
        description="receive-only call client with our own jitter buffer and a/v sync",
    )
    p.add_argument("--server", default="http://localhost:8087", help="call server url")
    p.add_argument("--room", required=True)
    p.add_argument("--name", default="python-rx")
    p.add_argument("--no-video", action="store_true", help="don't open video windows")
    p.add_argument("--no-audio", action="store_true", help="don't use the sound card")
    p.add_argument("--audio-device", help="sounddevice output device (name or index)")
    p.add_argument("--csv", help="write per-second buffer/sync stats here (put it under runs/)")
    p.add_argument("--duration", type=float, help="leave after this many seconds")
    p.add_argument("--min-delay", type=float, default=20, help="ms, jitter buffer floor")
    p.add_argument("--max-delay", type=float, default=1000, help="ms, jitter buffer ceiling")
    p.add_argument("--margin", type=float, default=10, help="ms added on top of measured jitter")
    p.add_argument("--relay", action="store_true", help="only use TURN (like ?relay=1)")
    p.add_argument("--insecure", action="store_true", help="accept a self-signed https cert")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


async def main(args):
    config = PlayoutConfig(
        min_delay=args.min_delay / 1000, max_delay=args.max_delay / 1000, margin=args.margin / 1000
    )
    from .outputs import NullAudioSink, NullVideoSink, SpeakerSink, WindowSink

    video = NullVideoSink() if args.no_video else WindowSink()
    if args.no_audio:
        audio = NullAudioSink()
    else:
        device = args.audio_device
        audio = SpeakerSink(device=int(device) if device and device.isdigit() else device)
    receiver = Receiver(config, video, audio, csv_path=args.csv)
    client = CallClient(
        args.server, args.room, args.name, receiver, args.relay, verify_tls=not args.insecure
    )
    signaling = asyncio.ensure_future(client.run())
    playout = asyncio.ensure_future(receiver.run(duration=args.duration))
    done, _ = await asyncio.wait({signaling, playout}, return_when=asyncio.FIRST_COMPLETED)
    for task in (signaling, playout):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await client.close()
    for task in done:
        if task.exception():
            raise task.exception()  # type: ignore[misc]


def run():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # aioice/aiortc are chatty at info
    for noisy in ("aioice", "aiortc"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    if sys.platform == "win32":
        # default windows timer is ~15 ms --> video ticks would bunch up. ask for 1 ms
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)  # type: ignore[attr-defined]
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(args))


if __name__ == "__main__":
    run()
