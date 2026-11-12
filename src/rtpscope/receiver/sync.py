# sync.py --> put audio and video on one clock (lip sync)
#
# audio and video are separate rtp streams with unrelated timestamps (random start, 48 kHz vs
# 90 kHz). the only link between them is the rtcp sender report (SR) each stream gets about once a
# second: "rtp timestamp R was sender wall clock (NTP) time N". with an SR for both streams every
# frame maps to the sender's wall clock, and then
#
#   play sender time t at our time t + offset,  offset = max(audio's offset, video's offset)
#
# the stream that needs less buffering waits for the other one, and both play the same sender
# instant at the same moment. until both SRs are in, each stream plays on its own (unsynced).
#
# aiortc throws away the rtp half of the SR and rebases pts to the first frame, hooks.py gets
# both back.

# seconds between the ntp epoch (1900) and the unix one (1970)
NTP_UNIX = 2_208_988_800


def ntp_to_unix(ntp64):
    """64-bit ntp timestamp (32.32 fixed point) --> unix seconds"""
    return (ntp64 >> 32) - NTP_UNIX + (ntp64 & 0xFFFFFFFF) / 2**32


class SenderClock:
    """rtp time of one stream --> sender wall clock

    origin: raw rtp timestamp of the first frame (aiortc's pts = rtp timestamp - origin, unwrapped)
    report: (unix seconds, raw rtp timestamp) from the latest sender report
    """

    def __init__(self, origin_source=None):
        self._origin: int | None = None
        # hooks.py passes a function that reads the origin out of aiortc once it's known
        self._origin_source = origin_source
        self.report: tuple[float, int] | None = None

    @property
    def origin(self) -> int | None:
        return self._origin_source() if self._origin_source else self._origin

    @origin.setter
    def origin(self, value: int | None):
        self._origin = value

    def on_report(self, ntp64, rtp_timestamp):
        self.report = (ntp_to_unix(ntp64), rtp_timestamp)

    @property
    def synced(self):
        return self.origin is not None and self.report is not None

    def media_time(self, pts, time_base):
        """(seconds, synced). synced --> sender's unix time. not synced --> seconds since the
        stream's first frame, only good for this stream on its own"""
        rate = 1 / time_base
        if not self.synced:
            return float(pts * time_base), False
        assert self.origin is not None and self.report is not None
        sr_time, sr_rtp = self.report
        # rtp timestamps are 32 bit and wrap --> signed distance from the report's timestamp
        diff = (self.origin + pts - sr_rtp + 2**31) % 2**32 - 2**31
        return sr_time + diff / float(rate), True
