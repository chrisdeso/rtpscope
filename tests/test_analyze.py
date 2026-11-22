import json

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("matplotlib")

from rtpscope.testbed import analyze, stats  # noqa: E402

# ---- a fake run with known causes, written through the real stats pipeline


GOOD = {
    "kbps": 1500.0,
    "fps": 30,
    "height": 720,
    "loss_pct": 0.0,
    "rtt_s": 0.010,
    "jb_ms": 20.0,
    "freezes": 0,
    "dropped": 0,
    "qlr": "none",
    "avail_kbps": 2500.0,
    "send_fps": 30,
}


def make_run(tmp_path, phases, netem="lo", interval=1.0):
    """phases: [(name, seconds, overrides, impairment dict)] --> a run folder.

    A sends video to B. counters accumulate like getStats does, rows go through StatsLog so the
    derived columns are computed exactly as in a real run.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    t0 = 1_000_000.0
    total = {"bytes": 0.0, "sent": 0.0, "recv": 0.0, "lost": 0.0, "frames": 0.0, "freezes": 0}
    total |= {"dropped": 0, "jb_delay": 0.0, "emitted": 0.0}
    net_rows = []
    tick = 0
    with (run_dir / "stats.csv").open("w", newline="") as f:
        log = stats.StatsLog(f)
        for name, seconds, overrides, impairment in phases:
            net_rows.append({"t_s": tick * interval, "step": name, **impairment})
            for _ in range(int(seconds / interval)):
                p = GOOD | overrides
                packets = p["kbps"] * 1000 / 8 / 1000 * interval  # ~1000-byte packets
                lost = packets * p["loss_pct"] / 100
                frames = p["fps"] * interval
                total["bytes"] += p["kbps"] * 1000 / 8 * interval
                total["sent"] += packets
                total["recv"] += packets - lost
                total["lost"] += lost
                total["frames"] += frames
                total["freezes"] += p["freezes"]
                total["dropped"] += p["dropped"]
                total["jb_delay"] += p["jb_ms"] / 1000 * frames
                total["emitted"] += frames
                now = t0 + tick * interval * 1000
                a = {
                    "now": now,
                    "pcs": [
                        {
                            "pc": 0,
                            "connectionState": "connected",
                            "stats": [
                                {"type": "transport", "id": "T", "selectedCandidatePairId": "CP"},
                                {"type": "candidate-pair", "id": "CP", "timestamp": now,
                                 "availableOutgoingBitrate": p["avail_kbps"] * 1000,
                                 "bytesSent": total["bytes"]},
                                {"type": "outbound-rtp", "id": "OV", "timestamp": now,
                                 "kind": "video", "ssrc": 7, "bytesSent": total["bytes"],
                                 "packetsSent": total["sent"], "framesPerSecond": p["send_fps"],
                                 "frameHeight": p["height"], "qualityLimitationReason": p["qlr"],
                                 "targetBitrate": p["kbps"] * 1000},
                                {"type": "remote-inbound-rtp", "id": "RV", "timestamp": now,
                                 "kind": "video", "ssrc": 7, "roundTripTime": p["rtt_s"]},
                            ],
                        }
                    ],
                }  # fmt: skip
                b = {
                    "now": now,
                    "pcs": [
                        {
                            "pc": 0,
                            "connectionState": "connected",
                            "stats": [
                                {"type": "inbound-rtp", "id": "IV", "timestamp": now,
                                 "kind": "video", "ssrc": 7, "bytesReceived": total["bytes"],
                                 "packetsReceived": total["recv"], "packetsLost": total["lost"],
                                 "framesPerSecond": p["fps"], "frameHeight": p["height"],
                                 "framesDecoded": total["frames"], "freezeCount": total["freezes"],
                                 "framesDropped": total["dropped"],
                                 "jitterBufferDelay": total["jb_delay"],
                                 "jitterBufferEmittedCount": total["emitted"]},
                            ],
                        }
                    ],
                }  # fmt: skip
                log.add(stats.rows_from_sample("A", a, t0, name))
                log.add(stats.rows_from_sample("B", b, t0, name))
                tick += 1
    cols = ["t_s", "step", "delay_ms", "jitter_ms", "loss_pct", "rate_kbit", "limit", "reorder_pct"]
    net = pd.DataFrame(net_rows).reindex(columns=cols).fillna(0)
    net.to_csv(run_dir / "network.csv", index=False)
    meta = {"run_id": "synthetic", "interval_s": interval, "netem": netem}
    (run_dir / "meta.json").write_text(json.dumps(meta))
    return run_dir


CLEAN = ("clean", 20, {}, {})


def faults_for(tmp_path, phase, **kw):
    run_dir = make_run(tmp_path, [CLEAN, phase, ("clean", 15, {}, {})], **kw)
    analyze.analyze(run_dir, plot=False)
    return pd.read_csv(run_dir / "faults.csv").fillna("")


def test_clean_run_has_no_drops(tmp_path):
    run_dir = make_run(tmp_path, [("clean", 40, {}, {})])
    analyze.analyze(run_dir, plot=False)
    assert pd.read_csv(run_dir / "faults.csv").empty


def test_loss_is_blamed_on_the_network(tmp_path):
    phase = (
        "lossy-5",
        15,
        {"loss_pct": 5, "freezes": 1, "fps": 12},
        {"loss_pct": 5, "delay_ms": 20},
    )
    faults = faults_for(tmp_path, phase)
    assert len(faults) == 1
    f = faults.iloc[0]
    assert f["stream"] == "A->B"
    assert f["blame"] == "network: packet loss" and f["path"] == "wire"
    assert "freeze" in f["symptoms"] and "fps" in f["symptoms"]
    assert f["start_s"] >= 20 and f["end_s"] < 35
    assert f["agrees"] == "yes"


def test_bandwidth_cap(tmp_path):
    phase = (
        "cap-500k",
        15,
        {"kbps": 400, "height": 360, "avail_kbps": 450, "qlr": "bandwidth"},
        {"rate_kbit": 500, "delay_ms": 20},
    )
    f = faults_for(tmp_path, phase).iloc[0]
    assert f["blame"] == "network: bandwidth (sender backed off)"
    assert "resolution" in f["symptoms"] and "720p->360p" in f["worst"]
    assert "2500 -> 450 kbit/s" in f["evidence"]
    assert f["agrees"] == "yes"


def test_delay_and_jitter(tmp_path):
    phase = (
        "jittery",
        15,
        {"rtt_s": 0.200, "jb_ms": 180, "fps": 15},
        {"delay_ms": 60, "jitter_ms": 40},
    )
    f = faults_for(tmp_path, phase).iloc[0]
    assert f["blame"] == "network: delay/jitter"
    assert "rtt 10 -> 200 ms" in f["evidence"] and "jitter buffer" in f["evidence"]
    assert f["agrees"] == "yes"


def test_sender_cpu_is_not_the_network(tmp_path):
    phase = ("clean-2", 15, {"qlr": "cpu", "height": 360, "send_fps": 15, "fps": 15}, {})
    f = faults_for(tmp_path, phase).iloc[0]
    assert f["blame"] == "sender: encoder CPU-limited" and f["path"] == "sender"
    assert f["agrees"] == "yes (not the network)"


def test_receiver_drops_on_a_clean_network(tmp_path):
    phase = ("clean-2", 15, {"dropped": 10, "fps": 18}, {})
    f = faults_for(tmp_path, phase).iloc[0]
    assert f["blame"] == "receiver: dropped frames before display" and f["path"] == "receiver"


def test_wrong_blame_is_reported(tmp_path):
    # loss on the wire but the fake network says it was clean --> flagged as a disagreement
    phase = ("clean-2", 15, {"loss_pct": 8, "fps": 10}, {})
    f = faults_for(tmp_path, phase).iloc[0]
    assert f["path"] == "wire" and f["agrees"] == "no (network was clean)"


def test_without_netem_nothing_to_agree_with(tmp_path):
    phase = ("lossy-5", 15, {"loss_pct": 5, "fps": 12}, {"loss_pct": 5})
    f = faults_for(tmp_path, phase, netem="off").iloc[0]
    assert f["blame"] == "network: packet loss" and f["agrees"] == ""


def test_outputs_and_plot(tmp_path):
    phases = [
        CLEAN,
        ("lossy-5", 15, {"loss_pct": 5, "fps": 12, "freezes": 1}, {"loss_pct": 5, "delay_ms": 20}),
        ("cap-500k", 15, {"kbps": 400, "height": 360, "avail_kbps": 450, "qlr": "bandwidth"},
         {"rate_kbit": 500}),
        ("clean", 15, {}, {}),
    ]  # fmt: skip
    run_dir = make_run(tmp_path, phases)
    written = analyze.analyze(run_dir)
    for key in ("timeline", "faults", "faults_md", "summary", "plot"):
        assert written[key].is_file() and written[key].stat().st_size > 0
    assert written["plot"].read_bytes()[:4] == b"\x89PNG"
    md = written["faults_md"].read_text()
    assert "network: packet loss" in md and "network: bandwidth" in md
    assert "2/2 drops" in md
    faults = pd.read_csv(written["faults"])
    # loss runs straight into the cap --> two events, split where the cause changes
    assert list(faults["blame"]) == [
        "network: packet loss",
        "network: bandwidth (sender backed off)",
    ]
    assert faults["end_s"].iloc[0] <= 36 and faults["start_s"].iloc[1] >= 35
    summary = pd.read_csv(written["summary"])
    assert list(summary["net_step"].unique()) == ["clean", "lossy-5", "cap-500k"]
    lossy = summary[summary["net_step"] == "lossy-5"].iloc[0]
    assert lossy["loss_pct"] == pytest.approx(5, abs=0.5)


def test_cli(tmp_path, capsys):
    run_dir = make_run(tmp_path, [("clean", 30, {}, {})])
    assert analyze.main([str(run_dir), "--no-plot"]) == 0
    assert "quality drops: 0" in capsys.readouterr().out
    assert analyze.main(["--latest", "--root", str(tmp_path), "--no-plot"]) == 0
    assert analyze.main([str(tmp_path / "missing")]) == 1
