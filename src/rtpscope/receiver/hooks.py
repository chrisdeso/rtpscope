# hooks.py --> two small patches to aiortc internals so sync.py can map rtp time to sender time
#
# 1. RTCRtpReceiver._handle_rtcp_packet reads sender reports but only keeps the ntp half.
#    we wrap it and keep (ntp, rtp timestamp) per receiver.
# 2. TimestampMapper rebases every frame's pts to the first rtp timestamp it sees and doesn't
#    expose it. we swap in a subclass that remembers it.
#
# written against aiortc 1.16 (pinned in pyproject). if a future aiortc renames these, install()
# warns and the receiver still plays, just without lip sync.

import logging
import weakref

from aiortc import rtcrtpreceiver
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.rtp import RtcpSrPacket

from .sync import SenderClock

log = logging.getLogger(__name__)

# receiver --> its SenderClock. weak so closed calls don't pile up
_clocks: "weakref.WeakKeyDictionary[RTCRtpReceiver, SenderClock]" = weakref.WeakKeyDictionary()
_installed = False


class _OriginMapper(rtcrtpreceiver.TimestampMapper):
    def __init__(self):
        super().__init__()
        self.first = None

    def map(self, timestamp):
        if self.first is None:
            self.first = timestamp
        return super().map(timestamp)


def install():
    """patch aiortc. call before any RTCPeerConnection is made, safe to call twice"""
    global _installed
    if _installed:
        return
    if not hasattr(rtcrtpreceiver, "TimestampMapper") or not hasattr(
        RTCRtpReceiver, "_handle_rtcp_packet"
    ):
        log.warning("aiortc internals changed, playing without lip sync")
        _installed = True
        return

    rtcrtpreceiver.TimestampMapper = _OriginMapper  # type: ignore[misc]
    original = RTCRtpReceiver._handle_rtcp_packet

    async def handle_rtcp_packet(self, packet):
        if isinstance(packet, RtcpSrPacket):
            info = packet.sender_info
            _clock(self).on_report(info.ntp_timestamp, info.rtp_timestamp)
        await original(self, packet)

    RTCRtpReceiver._handle_rtcp_packet = handle_rtcp_packet  # type: ignore[method-assign]
    _installed = True


# the mapper is a name-mangled private attribute --> look it up each time, it's cheap
def _mapper(receiver):
    return getattr(receiver, "_RTCRtpReceiver__timestamp_mapper", None)


def _clock(receiver):
    clock = _clocks.get(receiver)
    if clock is None:
        ref = weakref.ref(receiver)

        # the mapper only learns the origin when the first frame arrives --> ask it each time
        def origin():
            mapper = _mapper(ref())
            return getattr(mapper, "first", None)

        clock = _clocks[receiver] = SenderClock(origin_source=origin)
    return clock


def clock_for(receiver):
    """the SenderClock fed by this receiver's sender reports"""
    install()
    return _clock(receiver)
