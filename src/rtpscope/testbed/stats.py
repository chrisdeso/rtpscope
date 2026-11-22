# stats.py --> webrtc getStats() from every browser in the run, into one CSV
#
# the call app doesn't know it's being measured: HOOK_JS runs before the page's own scripts and
# swaps in an RTCPeerConnection that remembers every connection the page makes. each tick the
# runner calls COLLECT_JS in every browser, then StatsLog turns the reports into rows.
#
# one row per (browser, connection, stats object) per tick. long format on purpose: any side,
# any stream, any metric is one pandas filter away, and new metrics are just new columns.
import csv
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, TextIO

HOOK_JS = r"""
(() => {
  if (window.__rtpscope) return;
  const state = { pcs: [] };
  window.__rtpscope = state;
  const Original = window.RTCPeerConnection;
  if (!Original) return;
  class Recorded extends Original {
    constructor(...args) {
      super(...args);
      state.pcs.push(this);
    }
  }
  window.RTCPeerConnection = Recorded;
  window.webkitRTCPeerConnection = Recorded;

  // only what the logger reads --> keeps each tick small
  const KEEP = new Set([
    "outbound-rtp", "inbound-rtp", "remote-inbound-rtp", "remote-outbound-rtp",
    "candidate-pair", "local-candidate", "remote-candidate", "codec", "transport",
  ]);
  state.collect = async () => {
    const now = Date.now();
    const out = [];
    for (let i = 0; i < state.pcs.length; i++) {
      const pc = state.pcs[i];
      if (pc.signalingState === "closed") continue;
      const report = await pc.getStats();
      const stats = [];
      report.forEach((s) => { if (KEEP.has(s.type)) stats.push(s); });
      out.push({ pc: i, connectionState: pc.connectionState, stats });
    }
    return { now, pcs: out };
  };
  state.states = () => state.pcs.map((pc) => pc.connectionState);
})();
"""

COLLECT_JS = "window.__rtpscope ? window.__rtpscope.collect() : {now: Date.now(), pcs: []}"
STATES_JS = "window.__rtpscope ? window.__rtpscope.states() : []"

# csv column --> getStats field. same name across stat types where the meaning matches
FIELDS: dict[str, str] = {
    "ssrc": "ssrc",
    "mid": "mid",
    "rid": "rid",
    "bytes_sent": "bytesSent",
    "bytes_received": "bytesReceived",
    "packets_sent": "packetsSent",
    "packets_received": "packetsReceived",
    "packets_lost": "packetsLost",
    "fraction_lost": "fractionLost",
    "retransmitted_packets_sent": "retransmittedPacketsSent",
    "nack_count": "nackCount",
    "pli_count": "pliCount",
    "fir_count": "firCount",
    "target_bitrate": "targetBitrate",
    "quality_limitation_reason": "qualityLimitationReason",
    "frame_width": "frameWidth",
    "frame_height": "frameHeight",
    "frames_per_second": "framesPerSecond",
    "frames_encoded": "framesEncoded",
    "frames_sent": "framesSent",
    "key_frames_encoded": "keyFramesEncoded",
    "frames_received": "framesReceived",
    "frames_decoded": "framesDecoded",
    "frames_dropped": "framesDropped",
    "key_frames_decoded": "keyFramesDecoded",
    "freeze_count": "freezeCount",
    "total_freezes_duration_s": "totalFreezesDuration",
    "pause_count": "pauseCount",
    "jitter_buffer_delay_s": "jitterBufferDelay",
    "jitter_buffer_emitted_count": "jitterBufferEmittedCount",
    "concealed_samples": "concealedSamples",
    "total_samples_received": "totalSamplesReceived",
    "encoder_implementation": "encoderImplementation",
    "decoder_implementation": "decoderImplementation",
    "available_outgoing_bitrate": "availableOutgoingBitrate",
    "available_incoming_bitrate": "availableIncomingBitrate",
    "total_round_trip_time_s": "totalRoundTripTime",
    "round_trip_time_measurements": "roundTripTimeMeasurements",
}
# reported in seconds by webrtc, logged in ms because that's how everyone talks about them
MS_FIELDS: dict[str, str] = {
    "jitter_ms": "jitter",
    "rtt_ms": "roundTripTime",
    "current_rtt_ms": "currentRoundTripTime",
}

COLUMNS: list[str] = [
    # when + where
    "t_s",  # seconds since the run started (same clock for every browser)
    "wall_ms",  # unix ms of the stats report
    "side",  # which browser (A, B, ...)
    "pc",  # which RTCPeerConnection inside that browser (0 = first)
    "connection_state",
    # outbound-rtp | inbound-rtp | remote-inbound-rtp | remote-outbound-rtp | candidate-pair
    "type",
    "kind",  # audio | video
    "codec",
    "id",
    # what the fake network was doing at that moment
    "net_step",
    "net_desc",
    *FIELDS,
    *MS_FIELDS,
    # derived from the previous row with the same (side, pc, id)
    "bitrate_kbps",
    "packet_rate",
    "interval_loss_pct",
    "jitter_buffer_ms",
    # candidate-pair only
    "local_candidate_type",
    "remote_candidate_type",
    "protocol",
]

ROW_TYPES = {"outbound-rtp", "inbound-rtp", "remote-inbound-rtp", "remote-outbound-rtp"}


def rows_from_sample(
    side: str,
    sample: Mapping[str, Any],
    t0_ms: float,
    net_step: str = "",
    net_desc: str = "",
) -> list[dict[str, Any]]:
    """one browser's COLLECT_JS result --> raw rows (no derived columns yet)."""
    rows: list[dict[str, Any]] = []
    for pc in sample.get("pcs", []):
        stats = pc.get("stats", [])
        by_id = {s.get("id"): s for s in stats}
        codecs = {s["id"]: s.get("mimeType", "") for s in stats if s.get("type") == "codec"}
        # the pair actually carrying media is the one its transport points at
        selected = {
            s["selectedCandidatePairId"]
            for s in stats
            if s.get("type") == "transport" and s.get("selectedCandidatePairId")
        }

        for s in stats:
            kind = s.get("type")
            if kind == "candidate-pair":
                if not _pair_in_use(s, selected):
                    continue
            elif kind not in ROW_TYPES:
                continue
            wall_ms = float(s.get("timestamp") or sample.get("now") or 0)
            row: dict[str, Any] = {
                "t_s": round((wall_ms - t0_ms) / 1000, 3),
                "wall_ms": round(wall_ms, 1),
                "side": side,
                "pc": pc.get("pc", 0),
                "connection_state": pc.get("connectionState", ""),
                "type": kind,
                "kind": s.get("kind", ""),
                "codec": codecs.get(s.get("codecId"), "").split("/")[-1],
                "id": s.get("id", ""),
                "net_step": net_step,
                "net_desc": net_desc,
            }
            for column, key in FIELDS.items():
                if key in s:
                    row[column] = s[key]
            for column, key in MS_FIELDS.items():
                if isinstance(s.get(key), (int, float)):
                    row[column] = round(s[key] * 1000, 3)
            if kind == "candidate-pair":
                local = by_id.get(s.get("localCandidateId"), {})
                remote = by_id.get(s.get("remoteCandidateId"), {})
                row["local_candidate_type"] = local.get("candidateType", "")
                row["remote_candidate_type"] = remote.get("candidateType", "")
                row["protocol"] = local.get("protocol", "")
            rows.append(row)
    return rows


def _pair_in_use(pair: Mapping[str, Any], selected: set[str]) -> bool:
    if selected:
        return pair.get("id") in selected
    # no transport stats (older browsers) --> the nominated, working pair
    return bool(pair.get("nominated")) and pair.get("state") == "succeeded"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return None if math.isnan(value) else float(value)


class StatsLog:
    """adds the derived columns and appends rows to the csv as they come in."""

    def __init__(self, out: TextIO):
        self._writer = csv.DictWriter(out, fieldnames=COLUMNS, extrasaction="ignore")
        self._writer.writeheader()
        self._out = out
        self._prev: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
        self.rows_written = 0

    def add(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            key = (row.get("side"), row.get("pc"), row.get("id"))
            prev = self._prev.get(key)
            # same report twice (nothing new since the last tick) --> skip, it'd be a 0 s delta
            if prev is not None and prev.get("wall_ms") == row.get("wall_ms"):
                continue
            if prev is not None:
                derive(prev, row)
            self._prev[key] = row
            self._writer.writerow(row)
            self.rows_written += 1
        self._out.flush()


def derive(prev: Mapping[str, Any], row: dict[str, Any]) -> None:
    """rates over the interval since the previous row of the same stream."""
    dt_ms = (_number(row.get("wall_ms")) or 0) - (_number(prev.get("wall_ms")) or 0)
    if dt_ms <= 0:
        return
    dt_s = dt_ms / 1000

    def delta(column: str) -> float | None:
        now, before = _number(row.get(column)), _number(prev.get(column))
        if now is None or before is None:
            return None
        return now - before

    # senders (and the candidate pair, i.e. everything this browser puts on the wire) count
    # what went out; receivers count what came in
    sending = row.get("type") in ("outbound-rtp", "candidate-pair")
    d_bytes = delta("bytes_sent" if sending else "bytes_received")
    if d_bytes is not None and d_bytes >= 0:
        row["bitrate_kbps"] = round(d_bytes * 8 / dt_ms, 2)

    d_packets = delta("packets_sent" if sending else "packets_received")
    if d_packets is not None and d_packets >= 0:
        row["packet_rate"] = round(d_packets / dt_s, 2)

    # receiver's view of loss over just this interval (cumulative packets_lost hides changes)
    if row.get("type") == "inbound-rtp":
        d_lost = delta("packets_lost")
        if d_lost is not None and d_packets is not None and d_lost + d_packets > 0:
            row["interval_loss_pct"] = round(100 * max(d_lost, 0) / (d_lost + d_packets), 3)
        d_delay = delta("jitter_buffer_delay_s")
        d_emitted = delta("jitter_buffer_emitted_count")
        if d_delay is not None and d_emitted:
            row["jitter_buffer_ms"] = round(1000 * d_delay / d_emitted, 3)


def open_log(path: Path) -> tuple[TextIO, StatsLog]:
    out = path.open("w", newline="")
    return out, StatsLog(out)
