# analyze.py --> a run folder in, "whose fault was each quality drop" out
#
#   python -m rtpscope.testbed.analyze runs/<run id>      # or --latest
#
# writes next to stats.csv:
#   timeline.csv   one row per video stream (sender -> receiver) per tick, sender + receiver joined
#   faults.csv     one row per quality drop: what got worse, who's to blame, the evidence, and
#   faults.md        whether that matches what the fake network was doing (same table, for slides)
#   summary.csv    medians per network step per stream (the numbers for a results slide)
#   timeline.png   small multiples over time, network steps shaded, drops marked
#
# blame is rule based on purpose: every verdict comes with the numbers that triggered it, so
# you can check it by hand. thresholds are in THRESHOLDS.
#
# needs pandas + matplotlib: pip install -e '.[analysis]'
import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# ---- the rules

THRESHOLDS = {
    # ignore the first seconds: the bandwidth estimator is still ramping up
    "warmup_s": 6.0,
    # symptoms (receiver side, vs. the clean baseline)
    "fps_ratio": 0.7,  # fps below 70% of baseline
    "bitrate_ratio": 0.5,  # received video bitrate below half of baseline
    # causes
    "loss_pct": 1.0,  # receiver saw > 1% of packets lost in the interval
    "bwe_ratio": 0.5,  # sender's available bitrate estimate below half of baseline
    "rtt_ratio": 2.0,  # rtt over 2x baseline ...
    "rtt_floor_ms": 50.0,  # ... and at least this much above it
    "jb_ratio": 2.0,  # jitter buffer delay over 2x baseline ...
    "jb_floor_ms": 20.0,  # ... and at least this much above it
    # how far before a drop to look for its cause (congestion control reacts after the fact)
    "lookback_ticks": 3,
}

# category --> (path, plain words). order = priority when several causes show up at once:
# a network cause wins over a side effect it produces (loss --> fewer frames decoded, etc.)
CAUSES: dict[str, tuple[str, str]] = {
    "loss": ("wire", "network: packet loss"),
    "bandwidth": ("wire", "network: bandwidth (sender backed off)"),
    "delay": ("wire", "network: delay/jitter"),
    "sender-cpu": ("sender", "sender: encoder CPU-limited"),
    "sender-source": ("sender", "sender: fewer frames from the source"),
    "receiver": ("receiver", "receiver: dropped frames before display"),
}
UNEXPLAINED = ("unknown", "unexplained")
# the few words that fit on a bar in the plot
SHORT = {
    "network: packet loss": "loss",
    "network: bandwidth (sender backed off)": "bandwidth",
    "network: delay/jitter": "delay/jitter",
    "sender: encoder CPU-limited": "sender CPU",
    "sender: fewer frames from the source": "sender source",
    "receiver: dropped frames before display": "receiver",
    "unexplained": "?",
}


# ---- loading


@dataclass
class Run:
    path: Path
    stats: pd.DataFrame
    network: pd.DataFrame
    meta: dict

    @property
    def interval(self) -> float:
        return float(self.meta.get("interval_s", 1.0))

    @property
    def netem_applied(self) -> bool:
        return self.meta.get("netem") not in (None, "off", "dry-run")


def load_run(path: Path) -> Run:
    path = Path(path)
    stats_path = path / "stats.csv"
    if not stats_path.is_file():
        raise FileNotFoundError(f"no stats.csv in {path}")
    stats = pd.read_csv(stats_path, low_memory=False)
    net_path = path / "network.csv"
    network = pd.read_csv(net_path) if net_path.is_file() else pd.DataFrame()
    meta_path = path / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    return Run(path, stats, network, meta)


def latest_run(root: Path) -> Path:
    runs = sorted(p for p in Path(root).iterdir() if (p / "stats.csv").is_file())
    if not runs:
        raise FileNotFoundError(f"no runs with a stats.csv under {root}")
    return runs[-1]


# ---- one row per video stream per tick


def _per_tick(df: pd.DataFrame, interval: float) -> pd.DataFrame:
    df = df.copy()
    df["tick"] = (df["t_s"] / interval).round().astype(int)
    # two reports landing on the same tick --> keep the newer one
    return df.sort_values("t_s").drop_duplicates(["tick"], keep="last").set_index("tick")


def _diff(series: pd.Series) -> pd.Series:
    # counters only go up; a negative step means a reset, call it 0
    return series.diff().clip(lower=0)


def build_timeline(run: Run) -> pd.DataFrame:
    """join each receiver's inbound video with the sender's outbound for the same ssrc."""
    s = run.stats
    video = s[s["kind"] == "video"]
    inbound = video[video["type"] == "inbound-rtp"]
    outbound = video[video["type"] == "outbound-rtp"]
    remote_in = video[video["type"] == "remote-inbound-rtp"]
    # ssrc --> who sends it (outbound rows can be missing if a run died early; fall back to "?")
    sender_of = outbound.drop_duplicates("ssrc").set_index("ssrc")["side"].to_dict()

    frames = []
    for (receiver, ssrc), rx in inbound.groupby(["side", "ssrc"]):
        sender = sender_of.get(ssrc, "?")
        r = _per_tick(rx, run.interval)
        t = pd.DataFrame(index=r.index)
        t["t_s"] = r["tick"] if "tick" in r else r.index * run.interval
        t["t_s"] = r.index * run.interval
        t["stream"] = f"{sender}->{receiver}"
        t["sender"] = sender
        t["receiver"] = receiver
        t["net_step"] = r["net_step"]
        t["net_desc"] = r["net_desc"]
        t["recv_kbps"] = r["bitrate_kbps"]
        t["fps"] = r["frames_per_second"]
        # fps is missing while frozen; frames decoded per second fills the gap
        decoded_rate = _diff(r["frames_decoded"]) / run.interval
        t["fps"] = t["fps"].fillna(decoded_rate)
        t["height"] = r["frame_height"]
        t["loss_pct"] = r["interval_loss_pct"]
        t["jitter_ms"] = r["jitter_ms"]
        t["jitter_buffer_ms"] = r["jitter_buffer_ms"]
        t["freezes"] = _diff(r["freeze_count"])
        t["frames_dropped"] = _diff(r["frames_dropped"])

        tx = outbound[(outbound["side"] == sender) & (outbound["ssrc"] == ssrc)]
        if len(tx):
            o = _per_tick(tx, run.interval).reindex(t.index)
            t["send_kbps"] = o["bitrate_kbps"]
            t["target_kbps"] = o["target_bitrate"] / 1000
            t["send_fps"] = o["frames_per_second"]
            t["send_height"] = o["frame_height"]
            t["quality_limitation"] = o["quality_limitation_reason"]
            t["nacks"] = _diff(o["nack_count"])
            t["plis"] = _diff(o["pli_count"])
        pair = s[(s["side"] == sender) & (s["type"] == "candidate-pair")]
        if len(pair):
            p = _per_tick(pair, run.interval).reindex(t.index)
            t["available_kbps"] = p["available_outgoing_bitrate"] / 1000
        rtt = remote_in[(remote_in["side"] == sender) & (remote_in["ssrc"] == ssrc)]
        if len(rtt):
            # rtcp receiver reports come every few seconds --> hold the last value
            t["rtt_ms"] = _per_tick(rtt, run.interval)["rtt_ms"].reindex(t.index).ffill(limit=5)
        frames.append(t)
    if not frames:
        raise ValueError(f"{run.path}: no inbound video in stats.csv, did the call connect?")
    cols = [
        "t_s", "stream", "sender", "receiver", "net_step", "net_desc",
        "recv_kbps", "fps", "height", "loss_pct", "jitter_ms", "jitter_buffer_ms", "freezes",
        "frames_dropped", "send_kbps", "target_kbps", "available_kbps", "send_fps",
        "send_height", "quality_limitation", "nacks", "plis", "rtt_ms",
    ]  # fmt: skip
    out = pd.concat(frames).reset_index(drop=True)
    return out.reindex(columns=cols)


# ---- baseline, drops, blame


def baseline(stream: pd.DataFrame, run: Run) -> dict[str, float]:
    """medians over the clean start of the run (after warm-up, before the first impairment)."""
    warm = THRESHOLDS["warmup_s"]
    first_change = (
        float(run.network["t_s"].iloc[1])
        if len(run.network) > 1
        else float(stream["t_s"].max()) + 1
    )
    window = stream[(stream["t_s"] >= warm) & (stream["t_s"] < first_change)]
    if len(window) < 3:
        # no clean lead-in (a run that starts impaired) --> its first quarter, warm-up or not
        window = stream[stream["t_s"] <= stream["t_s"].quantile(0.25)]
    cols = [
        "recv_kbps",
        "fps",
        "height",
        "rtt_ms",
        "jitter_buffer_ms",
        "available_kbps",
        "send_fps",
    ]
    return {c: float(window[c].median()) for c in cols if c in window and window[c].notna().any()}


def symptoms(row: pd.Series, base: dict[str, float]) -> list[str]:
    found = []
    if row.get("freezes", 0) > 0:
        found.append("freeze")
    if (
        base.get("fps", 0) >= 5
        and pd.notna(row["fps"])
        and row["fps"] < THRESHOLDS["fps_ratio"] * base["fps"]
    ):
        found.append("fps")
    if base.get("height") and pd.notna(row["height"]) and row["height"] < base["height"]:
        found.append("resolution")
    if (
        base.get("recv_kbps", 0) > 0
        and pd.notna(row["recv_kbps"])
        and row["recv_kbps"] < THRESHOLDS["bitrate_ratio"] * base["recv_kbps"]
    ):
        found.append("bitrate")
    return found


def causes(window: pd.DataFrame, base: dict[str, float]) -> dict[str, str]:
    """category --> the evidence, for everything that looks wrong in this window."""
    th = THRESHOLDS
    found: dict[str, str] = {}

    loss = window["loss_pct"].max()
    if pd.notna(loss) and loss > th["loss_pct"]:
        found["loss"] = f"receiver lost {loss:.1f}% of packets"

    qlr = set(window["quality_limitation"].dropna())
    avail = window["available_kbps"].min()
    base_avail = base.get("available_kbps")
    low_bwe = bool(base_avail and pd.notna(avail) and avail < th["bwe_ratio"] * base_avail)
    if "bandwidth" in qlr or low_bwe:
        parts = []
        if low_bwe:
            parts.append(f"bandwidth estimate {base_avail:.0f} -> {avail:.0f} kbit/s")
        if "bandwidth" in qlr:
            parts.append("encoder limited by bandwidth")
        found["bandwidth"] = ", ".join(parts)

    delay_parts = []
    rtt, base_rtt = window["rtt_ms"].max(), base.get("rtt_ms")
    if (
        pd.notna(rtt)
        and base_rtt is not None
        and rtt > max(th["rtt_ratio"] * base_rtt, base_rtt + th["rtt_floor_ms"])
    ):
        delay_parts.append(f"rtt {base_rtt:.0f} -> {rtt:.0f} ms")
    jb, base_jb = window["jitter_buffer_ms"].max(), base.get("jitter_buffer_ms")
    if (
        pd.notna(jb)
        and base_jb is not None
        and jb > max(th["jb_ratio"] * base_jb, base_jb + th["jb_floor_ms"])
    ):
        delay_parts.append(f"jitter buffer {base_jb:.0f} -> {jb:.0f} ms")
    if delay_parts:
        found["delay"] = ", ".join(delay_parts)

    if "cpu" in qlr:
        found["sender-cpu"] = "encoder reported qualityLimitationReason=cpu"
    send_fps, base_send = window["send_fps"].min(), base.get("send_fps")
    if (
        "cpu" not in qlr
        and pd.notna(send_fps)
        and base_send
        and send_fps < th["fps_ratio"] * base_send
        and not found
    ):
        found["sender-source"] = (
            f"sender sent {base_send:.0f} -> {send_fps:.0f} fps, no network trouble"
        )

    dropped = window["frames_dropped"].sum()
    if dropped > 0 and "loss" not in found:
        found["receiver"] = f"receiver dropped {dropped:.0f} frames"
    return found


def injected(run: Run, start_s: float, end_s: float) -> tuple[str, set[str]]:
    """what the fake network was doing during [start, end], as cause categories."""
    if run.network.empty or not run.netem_applied:
        return ("not applied" if not run.network.empty else ""), set()
    net = run.network
    # the step in force at start_s, plus any that began before end_s
    active = net[(net["t_s"] <= end_s)]
    active = active[active["t_s"] >= active[active["t_s"] <= start_s]["t_s"].max()]
    expected: set[str] = set()
    for _, step in active.iterrows():
        if step.get("loss_pct", 0) > 0:
            expected.add("loss")
        if step.get("rate_kbit", 0) > 0:
            expected.add("bandwidth")
        if (
            step.get("delay_ms", 0) >= 50
            or step.get("jitter_ms", 0) > 0
            or step.get("reorder_pct", 0) > 0
        ):
            expected.add("delay")
    return " + ".join(active["step"].astype(str)), expected


def find_faults(timeline: pd.DataFrame, run: Run) -> pd.DataFrame:
    rows = []
    th = THRESHOLDS
    for stream, s in timeline.groupby("stream", sort=True):
        s = s.sort_values("t_s").reset_index(drop=True)
        base = baseline(s, run)
        # each flagged tick gets its own cause (this tick + the one before), so a drop that
        # changes cause halfway (loss step straight into a bandwidth cap) becomes two events
        flagged: list[tuple[int, list[str], str | None]] = []
        for i, row in s.iterrows():
            if row["t_s"] < th["warmup_s"]:
                continue
            sym = symptoms(row, base)
            if sym:
                found = causes(s.loc[max(0, i - 1) : i], base)
                flagged.append((i, sym, next((c for c in CAUSES if c in found), None)))
        events: list[list[tuple[int, list[str], str | None]]] = []
        for tick in flagged:
            prev = events[-1] if events else None
            if prev and tick[0] - prev[-1][0] <= 2 and tick[2] == prev[-1][2]:
                prev.append(tick)
            else:
                events.append([tick])
        # a one-tick event wedged against a neighbour is noise in the cause, not a new drop
        merged: list[list[tuple[int, list[str], str | None]]] = []
        for event in events:
            if merged and len(event) == 1 and event[0][0] - merged[-1][-1][0] <= 2:
                merged[-1].extend(event)
            elif merged and len(merged[-1]) == 1 and event[0][0] - merged[-1][-1][0] <= 2:
                merged[-1] = [*merged[-1], *event]
            else:
                merged.append(event)
        for event in merged:
            first, last = event[0][0], event[-1][0]
            span = s.loc[first:last]
            found = causes(s.loc[max(0, first - 1) : last], base)
            # the cause most of the event's ticks agree on; the lookback window as a fallback
            votes = [c for _, _, c in event if c]
            primary = max(set(votes), key=votes.count) if votes else None
            if primary is None:
                # nothing during the drop itself --> look further back (congestion control and
                # jitter buffers react a few seconds after the network does)
                wider = s.loc[max(0, first - int(th["lookback_ticks"])) : last]
                found = causes(wider, base)
                primary = next((c for c in CAUSES if c in found), None)
            path, blame = CAUSES[primary] if primary else UNEXPLAINED
            step, expected = injected(run, float(span["t_s"].iloc[0]), float(span["t_s"].iloc[-1]))
            if not step or step == "not applied":
                agrees = ""
            elif not expected:
                agrees = "no (network was clean)" if path == "wire" else "yes (not the network)"
            else:
                agrees = "yes" if primary in expected else "no"
            kinds = sorted({k for _, sym, _ in event for k in sym})
            rows.append(
                {
                    "stream": stream,
                    "start_s": round(float(span["t_s"].iloc[0]), 1),
                    "end_s": round(float(span["t_s"].iloc[-1]), 1),
                    "duration_s": round(
                        float(span["t_s"].iloc[-1] - span["t_s"].iloc[0]) + run.interval, 1
                    ),
                    "symptoms": ", ".join(kinds),
                    "worst": _worst(span, base),
                    "blame": blame,
                    "path": path,
                    "evidence": "; ".join(f"{k}: {v}" for k, v in found.items()) or "-",
                    "net_step": step,
                    "agrees": agrees,
                }
            )
    columns = [
        "stream", "start_s", "end_s", "duration_s", "symptoms", "worst", "blame", "path",
        "evidence", "net_step", "agrees",
    ]  # fmt: skip
    return pd.DataFrame(rows, columns=columns)


def _worst(span: pd.DataFrame, base: dict[str, float]) -> str:
    """only the metrics that actually got worse, baseline -> worst value."""
    th = THRESHOLDS
    parts = []
    fps = span["fps"].min()
    if base.get("fps") and pd.notna(fps) and fps < th["fps_ratio"] * base["fps"]:
        parts.append(f"fps {base['fps']:.0f}->{fps:.0f}")
    height = span["height"].min()
    if base.get("height") and pd.notna(height) and height < base["height"]:
        parts.append(f"{base['height']:.0f}p->{height:.0f}p")
    kbps = span["recv_kbps"].min()
    if base.get("recv_kbps") and pd.notna(kbps) and kbps < th["bitrate_ratio"] * base["recv_kbps"]:
        parts.append(f"{base['recv_kbps']:.0f}->{kbps:.0f} kbit/s")
    if span["freezes"].sum() > 0:
        parts.append(f"{span['freezes'].sum():.0f} freeze(s)")
    return ", ".join(parts)


def summarize_steps(timeline: pd.DataFrame) -> pd.DataFrame:
    """medians per network step per stream, in the order the steps happened."""
    order = {step: i for i, step in enumerate(timeline.sort_values("t_s")["net_step"].unique())}
    t = timeline[timeline["t_s"] >= THRESHOLDS["warmup_s"]]
    g = t.groupby(["net_step", "stream"], sort=False)
    out = g.agg(
        seconds=("t_s", "count"),
        recv_kbps=("recv_kbps", "median"),
        fps=("fps", "median"),
        height=("height", "median"),
        loss_pct=("loss_pct", "mean"),
        rtt_ms=("rtt_ms", "median"),
        jitter_buffer_ms=("jitter_buffer_ms", "median"),
        freezes=("freezes", "sum"),
    ).reset_index()
    out["_order"] = out["net_step"].map(order)
    # repeated step names (clean at start and end) are one row: the medians cover both visits
    return out.sort_values(["_order", "stream"]).drop(columns="_order").round(2)


# ---- plot

# validated categorical order (dataviz palette slots 1-4), one per stream, fixed
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e0", "#fcfcfb"
FAULT = "#d03b3b"  # status: critical


def plot_timeline(timeline: pd.DataFrame, faults: pd.DataFrame, run: Run, out: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # column, title, smallest y top (so a flat 0 doesn't get a 0-0.04 axis)
    panels = [
        ("recv_kbps", "received video, kbit/s", 100),
        ("fps", "received frames per second", 30),
        ("height", "received resolution, px tall", 720),
        ("loss_pct", "packet loss, % of packets", 5),
        ("rtt_ms", "round-trip time, ms", 20),
        ("jitter_buffer_ms", "jitter buffer delay, ms", 50),
    ]
    panels = [p for p in panels if timeline[p[0]].notna().any()]
    streams = sorted(timeline["stream"].unique())
    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.edgecolor": GRID,
            "xtick.color": INK_2,
            "ytick.color": INK_2,
            "text.color": INK,
        }
    )
    ratios = [3] * len(panels) + [0.35 * len(streams) + 0.3]
    fig, axes = plt.subplots(
        len(panels) + 1,
        1,
        figsize=(11, 1.55 * len(panels) + 1.8),
        sharex=True,
        facecolor=SURFACE,
        gridspec_kw={"height_ratios": ratios, "hspace": 0.55},
    )
    axes = list(axes)
    plot_axes, strip = axes[:-1], axes[-1]
    t_end = float(timeline["t_s"].max()) + run.interval

    # network steps --> alternating light bands, named once above the top panel
    steps = run.network if not run.network.empty else pd.DataFrame({"t_s": [0.0], "step": ["run"]})
    bounds = [*steps["t_s"].astype(float).tolist(), t_end]
    for ax in axes:
        ax.set_facecolor(SURFACE)
        for i in range(len(steps)):
            if i % 2:
                ax.axvspan(bounds[i], bounds[i + 1], color="#f0efec", lw=0, zorder=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(length=0)
    for i in range(len(steps)):
        plot_axes[0].annotate(
            str(steps["step"].iloc[i]),
            ((bounds[i] + bounds[i + 1]) / 2, 1.0),
            xycoords=("data", "axes fraction"),
            xytext=(0, 14),
            textcoords="offset points",
            ha="center",
            fontsize=8.5,
            color=INK,
        )

    for ax, (col, title, floor) in zip(plot_axes, panels, strict=True):
        ax.grid(axis="y", color=GRID, lw=0.6)
        ax.set_axisbelow(True)
        ends = []
        for k, stream in enumerate(streams):
            s = timeline[timeline["stream"] == stream].sort_values("t_s")
            ax.plot(s["t_s"], s[col], color=SERIES[k % len(SERIES)], lw=1.6, label=stream)
            last = s[["t_s", col]].dropna().tail(1)
            if len(last):
                ends.append([float(last[col].iloc[0]), stream])
        top = max(floor, float(timeline[col].max(skipna=True)) * 1.15)
        ax.set_ylim(0, top)
        ax.set_title(title, loc="left", fontsize=8.5, color=INK_2, pad=3)
        # direct labels at the line ends, pushed apart so overlapping lines stay readable
        ends.sort()
        gap = top * 0.16
        for j in range(1, len(ends)):
            ends[j][0] = max(ends[j][0], ends[j - 1][0] + gap)
        for y, stream in ends:
            ax.annotate(
                stream,
                (t_end, min(y, top)),
                xytext=(4, 0),
                textcoords="offset points",
                va="center",
                fontsize=7.5,
                color=INK_2,
                annotation_clip=False,
            )

    # quality drops --> their own strip, one row per stream, and a faint band on every panel
    strip.set_yticks(range(len(streams)), streams)
    strip.set_ylim(-0.7, len(streams) - 0.3)
    strip.invert_yaxis()
    strip.set_title(
        f"quality drops ({len(faults)}), labeled with whose fault",
        loc="left",
        fontsize=8.5,
        color=INK_2,
        pad=3,
    )
    for _, f in faults.iterrows():
        k = streams.index(f["stream"]) if f["stream"] in streams else 0
        start = float(f["start_s"])
        width = float(f["end_s"]) - start + run.interval
        # a small inset each side --> back-to-back drops read as separate bars
        inset = min(0.3, width * 0.1)
        strip.broken_barh([(start + inset, width - 2 * inset)], (k - 0.32, 0.64), color=FAULT)
        strip.text(
            start + width / 2,
            k,
            SHORT.get(f["blame"], f["blame"]),
            ha="center",
            va="center",
            fontsize=7.5,
            color="#ffffff",
            clip_on=True,
        )
    strip.set_xlim(0, t_end)
    strip.set_xlabel("seconds since the scenario started", color=INK_2)

    if len(streams) > 1:
        handles, labels = plot_axes[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="upper right", frameon=False, ncol=len(streams), fontsize=8)
    else:
        # one stream needs no legend, just say which way the video goes
        note = f"video {streams[0]} (sender->receiver)"
        fig.text(0.94, 0.965, note, ha="right", fontsize=8, color=INK_2)
    title = str(run.meta.get("run_id", run.path.name))
    if not run.netem_applied:
        title += "  (fake network not applied)"
    fig.suptitle(title, x=0.01, ha="left", fontsize=10, color=INK)
    fig.subplots_adjust(left=0.06, right=0.94, top=0.9, bottom=0.07)
    path = out / "timeline.png"
    fig.savefig(path, dpi=160, facecolor=SURFACE)
    plt.close(fig)
    return path


# ---- output


def to_markdown(df: pd.DataFrame) -> str:
    if df.empty:
        return "_no quality drops found_\n"
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, row in df.iterrows():
        cells = [str(row[c]).replace("|", "/") for c in cols]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def accuracy(faults: pd.DataFrame) -> str:
    judged = faults[faults["agrees"] != ""]
    if judged.empty:
        return ""
    right = judged["agrees"].str.startswith("yes").sum()
    return f"blame matched the injected network condition for {right}/{len(judged)} drops"


def analyze(path: Path, plot: bool = True) -> dict[str, Path]:
    run = load_run(path)
    timeline = build_timeline(run)
    faults = find_faults(timeline, run)
    summary = summarize_steps(timeline)
    out = run.path
    written = {
        "timeline": out / "timeline.csv",
        "faults": out / "faults.csv",
        "faults_md": out / "faults.md",
        "summary": out / "summary.csv",
    }
    timeline.to_csv(written["timeline"], index=False)
    faults.to_csv(written["faults"], index=False)
    note = accuracy(faults)
    written["faults_md"].write_text(
        f"# Quality drops: {run.meta.get('run_id', out.name)}\n\n"
        + to_markdown(faults)
        + (f"\n{note}\n" if note else "")
    )
    summary.to_csv(written["summary"], index=False)
    if plot:
        written["plot"] = plot_timeline(timeline, faults, run, out)

    print(f"{out}")
    with pd.option_context(
        "display.width", 200, "display.max_columns", 20, "display.max_colwidth", 60
    ):
        print("\nper network step (medians):")
        print(summary.to_string(index=False))
        print(f"\nquality drops: {len(faults)}")
        if len(faults):
            print(
                faults[
                    ["stream", "start_s", "end_s", "symptoms", "blame", "net_step", "agrees"]
                ].to_string(index=False)
            )
        if note:
            print(note)
    print("\nwrote " + ", ".join(p.name for p in written.values()))
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rtpscope: fault table + plots for a testbed run")
    parser.add_argument("runs", nargs="*", type=Path, help="run folder(s) with a stats.csv")
    parser.add_argument("--latest", action="store_true", help="the newest run under --root")
    parser.add_argument("--root", type=Path, default=Path("runs"))
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args(argv)
    try:
        paths = list(args.runs) or ([latest_run(args.root)] if args.latest else [])
        if not paths:
            parser.error("give a run folder, or --latest")
        for path in paths:
            analyze(path, plot=not args.no_plot)
    except (FileNotFoundError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
