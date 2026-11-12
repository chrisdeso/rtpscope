import asyncio
import fractions
import time

import aiohttp
import numpy as np
from aiohttp.test_utils import TestServer
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.codecs import get_decoder, get_encoder
from aiortc.codecs.vpx import vp8_depayload
from aiortc.jitterbuffer import JitterFrame
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from av import VideoFrame

from rtpscope.call.server import create_call_app
from rtpscope.receiver import pattern
from rtpscope.receiver.buffer import DelayEstimator, FrameQueue, PlayoutClock, SampleQueue
from rtpscope.receiver.client import CallClient, parse_candidate, ws_url
from rtpscope.receiver.outputs import NullAudioSink, NullVideoSink
from rtpscope.receiver.player import Receiver
from rtpscope.receiver.sync import NTP_UNIX, SenderClock, ntp_to_unix
from rtpscope.receiver.testsource import BeepAudioTrack, PatternVideoTrack
from rtpscope.receiver.testsource import SenderClock as PatternClock

# ---- test pattern


def test_barcode_round_trip():
    ms = 1_762_871_234_567
    img = pattern.draw(np.full((360, 640), 90, dtype=np.uint8), ms)
    assert pattern.read_barcode(img) == ms


def test_barcode_rejects_corruption():
    img = pattern.draw(np.full((360, 640), 90, dtype=np.uint8), 1_762_871_234_567)
    # flip one data cell --> crc has to catch it
    x0, x1 = round(10 * 640 / pattern.CELLS), round(11 * 640 / pattern.CELLS)
    img[: round(0.06 * 360), x0:x1] = 255 - img[5, (x0 + x1) // 2]
    assert pattern.read_barcode(img) is None
    assert pattern.read_barcode(np.zeros((360, 640), dtype=np.uint8)) is None


# the barcode has to survive the ladder: scaled down, then through a real vp8 encode/decode
def test_barcode_survives_vp8_at_low_res():
    codec = RTCRtpCodecParameters(mimeType="video/VP8", clockRate=90000)
    encoder, decoder = get_encoder(codec), get_decoder(codec)
    ms0 = 1_762_871_234_000
    decoded = []
    for i in range(10):
        img = np.full((270, 480, 3), 60, dtype=np.uint8)
        pattern.draw(img, ms0 + 33 * i)
        frame = VideoFrame.from_ndarray(img, format="bgr24")
        frame.pts, frame.time_base = i * 3000, fractions.Fraction(1, 90000)
        payloads, timestamp = encoder.encode(frame)
        data = b"".join(vp8_depayload(p) for p in payloads)
        for out in decoder.decode(JitterFrame(data=data, timestamp=timestamp)):
            assert isinstance(out, VideoFrame)
            decoded.append(pattern.read_barcode(out.to_ndarray(format="gray")))
    assert decoded[-1] == ms0 + 33 * 9
    assert sum(d is not None for d in decoded) >= 8


def test_flash_follows_the_second():
    img = np.zeros((360, 640), dtype=np.uint8)
    assert pattern.read_flash(pattern.draw(img, 5_000_050))
    assert not pattern.read_flash(pattern.draw(img, 5_000_500))


def test_beep_onset_is_sample_accurate():
    rate = 48000
    detector = pattern.BeepDetector(rate)
    t = np.arange(rate) / rate
    onset = 0.3127
    beep = (t >= onset) & (t < onset + 0.1)
    noise = np.random.default_rng(1).normal(0, 0.01, rate)
    mono = np.where(beep, 0.5 * np.sin(2 * np.pi * 1000 * t), 0) + noise
    found = []
    for i in range(0, rate, 480):  # sound card sized chunks
        found += detector.feed(mono[i : i + 480], 100.0 + i / rate)
    assert len(found) == 1
    assert abs(found[0] - (100.0 + onset)) < 0.002


# ---- jitter buffer


def test_target_tracks_jitter():
    rng = np.random.default_rng(0)
    smooth, wobbly = DelayEstimator(), DelayEstimator()
    for i in range(300):
        t = i / 30
        smooth.add(t, t + 5.0)
        wobbly.add(t, t + 5.0 + rng.uniform(0, 0.080))
    assert smooth.target == smooth.min_delay
    # 95th percentile of uniform(0, 80 ms) + 10 ms margin
    assert 0.075 < wobbly.target < 0.095
    wobbly_offset, smooth_offset = wobbly.desired_offset(), smooth.desired_offset()
    assert wobbly_offset is not None and smooth_offset is not None
    assert wobbly_offset > smooth_offset


def test_clock_rises_fast_and_falls_slowly():
    clock = PlayoutClock(decay=0.01)
    clock.update(0.100, now=0.0)
    clock.update(0.300, now=1.0)
    assert clock.offset == 0.300
    clock.update(0.100, now=2.0)
    assert clock.offset is not None and abs(clock.offset - 0.290) < 1e-9
    assert clock.play_time(10.0) == 10.0 - clock.offset


def test_frame_queue_late_and_skipped():
    q = FrameQueue()
    for t in (0.0, 0.033, 0.066):
        q.push(t, f"f{t}", play_t=-1)
    assert q.pop_due(0.05) == (0.033, "f0.033")
    assert q.dropped == 1  # f0.0 was due but superseded
    assert q.push(0.020, "old", play_t=0.05) is False  # older than what's on screen
    assert q.late == 1 and q.dropped == 2
    assert q.depth(0.05) > 0


def test_sample_queue_conceals_gaps_and_drops_late():
    q = SampleQueue(rate=1000, channels=1, jump=0.5, nudge=0.5)
    q.push(0, np.ones((10, 1), dtype=np.float32))
    q.push(20, np.ones((10, 1), dtype=np.float32) * 2)
    out = q.read(0, 30)[:, 0]
    assert list(out[:10]) == [1] * 10 and list(out[10:20]) == [0] * 10 and out[25] == 2
    assert q.concealed == 10
    assert q.push(5, np.ones((10, 1), dtype=np.float32)) is False
    assert q.late == 1


def test_sample_queue_follows_clock():
    q = SampleQueue(rate=48000, channels=1)
    q.push(0, np.zeros((48000, 1), dtype=np.float32))
    q.read(0, 480)
    q.read(480 + 5000, 480)  # clock moved 100 ms --> jump
    assert q.cursor == 5480 + 480
    q.read(5960 + 300, 480)  # 6 ms drift --> nudged, not jumped
    assert 5960 + 480 < q.cursor < 5960 + 300 + 480


# ---- sync


def test_sender_clock_maps_rtp_to_sender_time():
    clock = SenderClock()
    assert clock.media_time(4800, fractions.Fraction(1, 48000)) == (0.1, False)
    unix = 1_762_871_234.5
    ntp = round((unix + NTP_UNIX) * 2**32)
    assert abs(ntp_to_unix(ntp) - unix) < 1e-6
    # first rtp timestamp just below the 32 bit wrap, report taken after the wrap
    clock.origin = 2**32 - 4800
    clock.on_report(ntp, 4800)
    t, synced = clock.media_time(9600, fractions.Fraction(1, 48000))
    assert synced and abs(t - unix) < 1e-6
    t, _ = clock.media_time(0, fractions.Fraction(1, 48000))
    assert abs(t - (unix - 0.2)) < 1e-6


# ---- whole thing: a python "browser" sends the pattern, the receiver joins through the server


async def _pattern_sender(server_url, room):
    """joins like call.js: waits for the receiver's offer and answers with pattern tracks"""
    clock = PatternClock()
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(ws_url(server_url))
    await ws.send_json({"type": "join", "room": room, "name": "pattern"})
    assert (await ws.receive_json())["type"] == "welcome"
    pcs = []

    async def loop():
        async for msg in ws:
            message = msg.json()
            if message["type"] == "peer-joined":
                pc = RTCPeerConnection()
                pc.addTrack(BeepAudioTrack(clock))
                pc.addTrack(PatternVideoTrack(clock))
                pcs.append(pc)
            elif message["type"] == "signal" and "sdp" in message["data"]:
                pc = pcs[-1]
                sdp = message["data"]["sdp"]
                await pc.setRemoteDescription(RTCSessionDescription(**sdp))
                await pc.setLocalDescription(await pc.createAnswer())
                answer = {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp}
                await ws.send_json(
                    {"type": "signal", "to": message["from"], "data": {"sdp": answer}}
                )

    task = asyncio.ensure_future(loop())

    async def close():
        task.cancel()
        for pc in pcs:
            await pc.close()
        await ws.close()
        await session.close()

    return close


def test_receiver_plays_a_call_in_sync():
    async def main():
        server = TestServer(create_call_app())
        await server.start_server()
        url = str(server.make_url("/"))
        close_sender = await _pattern_sender(url, "lab")
        receiver = Receiver(video_sink=NullVideoSink(), audio_sink=NullAudioSink())
        client = CallClient(url, "lab", "rx", receiver)
        signaling = asyncio.ensure_future(client.run())
        await receiver.run(duration=7)
        signaling.cancel()
        await client.close()
        await close_sender()
        await server.close()
        return receiver.history

    started = time.monotonic()
    history = asyncio.run(main())
    assert time.monotonic() - started < 30
    last = {row["kind"]: row for row in history[-2:]}
    video, audio = last["video"], last["audio"]
    assert video["synced"] and audio["synced"]
    assert video["frames"] > 120 and audio["frames"] > 250
    # same machine --> barcode clock is our clock, so e2e delay is real
    e2e = [r["e2e_ms"] for r in history if r["e2e_ms"] is not None]
    assert e2e and 0 < np.median(e2e) < 300
    # flash vs beep at the output, once synced
    av = [r["av_sync_ms"] for r in history if r["synced"] and r["av_sync_ms"] is not None]
    assert len(av) >= 3
    assert abs(np.median(av)) < 45


def test_parse_browser_candidate():
    candidate = parse_candidate(
        {
            "candidate": "candidate:1 1 udp 2122260223 192.0.2.10 54321 typ host generation 0",
            "sdpMid": "0",
            "sdpMLineIndex": 0,
        }
    )
    assert candidate is not None
    assert (candidate.ip, candidate.port, candidate.sdpMid) == ("192.0.2.10", 54321, "0")
    assert parse_candidate({"candidate": ""}) is None
    assert ws_url("https://call.example.com") == "wss://call.example.com/ws"
