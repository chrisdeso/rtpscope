import csv
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from rtpscope.testbed import browser, netem, run, stats
from rtpscope.testbed.netem import (
    PROFILES,
    SCENARIOS,
    Impairment,
    Netem,
    NetemError,
    Scenario,
    Step,
    change_command,
    load_scenario,
    setup_commands,
)

# ---- fake network


def test_netem_args():
    assert Impairment().netem_args() == ["delay", "0ms"]
    assert Impairment(delay_ms=20, loss_pct=5).netem_args() == ["delay", "20ms", "loss", "5%"]
    assert Impairment(delay_ms=60, jitter_ms=40).netem_args() == [
        "delay", "60ms", "40ms", "distribution", "normal",
    ]  # fmt: skip
    assert Impairment(delay_ms=20, rate_kbit=500, limit=30).netem_args() == [
        "delay", "20ms", "rate", "500kbit", "limit", "30",
    ]  # fmt: skip
    assert Impairment(delay_ms=2.5, loss_pct=0.5).netem_args() == ["delay", "2.5ms", "loss", "0.5%"]


@pytest.mark.parametrize(
    "bad",
    [
        {"loss_pct": -1},
        {"loss_pct": 101},
        {"jitter_ms": 10},  # jitter without delay
        {"reorder_pct": 5},  # reorder without delay
        {"rate_kbit": -5},
    ],
)
def test_bad_impairments_rejected(bad):
    with pytest.raises(ValueError):
        Impairment(**bad)


def test_describe():
    assert Impairment().describe() == "clean"
    assert PROFILES["3g"].describe() == "100ms±20, 1% loss, 750 kbit/s, queue 50"


def test_setup_only_sends_udp_into_netem():
    cmds = [" ".join(c) for c in setup_commands("lo", PROFILES["lossy-5"])]
    assert cmds[0] == "tc qdisc add dev lo root handle 1: prio bands 4"
    assert cmds[1] == "tc qdisc add dev lo parent 1:4 handle 40: netem delay 20ms loss 5%"
    # dns first, to an untouched band
    assert "dport 53 0xffff flowid 1:1" in cmds[2]
    assert "protocol 17 0xff flowid 1:4" in cmds[3]
    assert "protocol ipv6" in cmds[4] and cmds[4].endswith("flowid 1:4")
    # nothing ever matches tcp --> signaling can't be broken by the fake network
    assert not any("protocol 6 " in c for c in cmds)


def test_change_always_says_the_rate():
    # kernel keeps the old rate unless a change carries one --> uncapping must be explicit
    uncapped = change_command("lo", PROFILES["jittery"])
    assert uncapped[:9] == ["tc", "qdisc", "change", "dev", "lo", "parent", "1:4", "handle", "40:"]
    assert uncapped[-2:] == ["rate", netem.UNCAPPED]
    capped = change_command("lo", PROFILES["cap-1m"])
    assert capped.count("rate") == 1 and "1000kbit" in capped


class FakeTc:
    def __init__(self, fail_on: str | None = None):
        self.calls: list[str] = []
        self.fail_on = fail_on

    def __call__(self, cmd):
        line = " ".join(cmd)
        self.calls.append(line)
        failed = self.fail_on is not None and self.fail_on in line
        return subprocess.CompletedProcess(cmd, 1 if failed else 0, "", "boom" if failed else "")


def test_netem_apply_change_clear():
    tc = FakeTc()
    net = Netem("lo", runner=tc)
    net.apply(PROFILES["lossy-2"])
    # stale setup from a crashed run is cleared first, then the 5 setup commands
    assert tc.calls[0] == "tc qdisc del dev lo root"
    assert len(tc.calls) == 6 and net.active
    net.apply(PROFILES["cap-500k"])
    assert tc.calls[-1].startswith("tc qdisc change dev lo parent 1:4 handle 40: netem")
    net.clear()
    assert tc.calls[-1] == "tc qdisc del dev lo root" and not net.active


def test_netem_failed_setup_cleans_up():
    tc = FakeTc(fail_on="netem")
    net = Netem("lo", runner=tc)
    with pytest.raises(NetemError, match="boom"):
        net.apply(PROFILES["lossy-2"])
    assert tc.calls[-1] == "tc qdisc del dev lo root"
    assert not net.active


def test_netem_dry_run_prints(capsys):
    net = Netem("eth9", dry_run=True)
    net.apply(PROFILES["laggy"])
    net.clear()
    out = capsys.readouterr().out
    assert "tc qdisc add dev eth9 parent 1:4 handle 40: netem delay 150ms" in out
    assert out.strip().endswith("tc qdisc del dev eth9 root")


def test_netem_cli(capsys):
    assert netem.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "loss-staircase" in out and "cap-500k" in out
    assert netem.main(["apply", "lossy-5", "--dry-run"]) == 0
    assert "netem delay 20ms loss 5%" in capsys.readouterr().out


# ---- scenarios


def test_builtin_scenarios_start_and_end_clean():
    for name, scenario in SCENARIOS.items():
        assert scenario.steps[0].name == "clean", name
        assert scenario.steps[-1].name == "clean", name


def test_step_at():
    sc = SCENARIOS["loss-staircase"]
    assert sc.step_at(0).name == "clean"
    assert sc.step_at(14.99).name == "clean"
    assert sc.step_at(15).name == "lossy-2"
    assert sc.step_at(74).name == "clean"


@pytest.mark.parametrize(
    "steps,duration",
    [
        ((), 10),
        ((Step(5, "x", Impairment()),), 10),  # doesn't start at 0
        ((Step(0, "a", Impairment()), Step(0, "b", Impairment())), 10),  # same time twice
        ((Step(0, "a", Impairment()), Step(20, "b", Impairment())), 10),  # ends before last step
    ],
)
def test_bad_scenarios_rejected(steps, duration):
    with pytest.raises(ValueError):
        Scenario("bad", steps, duration)


def test_load_scenario(tmp_path):
    assert load_scenario("mixed").name == "mixed"
    assert load_scenario("mixed", 60).duration_s == 60
    short = load_scenario("loss-staircase", 20)
    assert [s.name for s in short.steps] == ["clean", "lossy-2"]
    held = load_scenario("lossy-5", 20)
    assert held.duration_s == 20 and held.step_at(19).impairment == PROFILES["lossy-5"]

    path = tmp_path / "custom.json"
    path.write_text(
        json.dumps(
            {
                "duration_s": 30,
                "steps": [
                    {"at_s": 0, "profile": "clean"},
                    {"at_s": 10, "name": "bursty", "delay_ms": 50, "loss_pct": 3},
                ],
            }
        )
    )
    custom = load_scenario(str(path))
    assert custom.name == "custom"
    assert custom.step_at(12).name == "bursty"
    assert custom.step_at(12).impairment == Impairment(delay_ms=50, loss_pct=3)

    with pytest.raises(ValueError, match="unknown scenario"):
        load_scenario("nope")
    path.write_text(json.dumps({"duration_s": 5, "steps": [{"at_s": 0, "profile": "x"}]}))
    with pytest.raises(ValueError, match="unknown profile"):
        load_scenario(str(path))


# ---- stats


def sample(now, video_bytes, video_packets, lost, pair="CP1"):
    """what COLLECT_JS returns, trimmed to what matters."""
    return {
        "now": now,
        "pcs": [
            {
                "pc": 0,
                "connectionState": "connected",
                "stats": [
                    {"type": "codec", "id": "C1", "mimeType": "video/VP8"},
                    {"type": "transport", "id": "T1", "selectedCandidatePairId": pair},
                    {
                        "type": "inbound-rtp", "id": "IV", "timestamp": now, "kind": "video",
                        "codecId": "C1", "ssrc": 42, "bytesReceived": video_bytes,
                        "packetsReceived": video_packets, "packetsLost": lost, "jitter": 0.004,
                        "framesDecoded": 30, "jitterBufferDelay": 1.5,
                        "jitterBufferEmittedCount": 30,
                    },
                    {
                        "type": "outbound-rtp", "id": "OV", "timestamp": now, "kind": "video",
                        "bytesSent": video_bytes, "packetsSent": video_packets,
                        "qualityLimitationReason": "bandwidth",
                    },
                    {"type": "remote-inbound-rtp", "id": "RV", "timestamp": now, "kind": "video",
                     "roundTripTime": 0.0425, "fractionLost": 0.05},
                    {"type": "candidate-pair", "id": "CP1", "timestamp": now, "nominated": True,
                     "state": "succeeded", "localCandidateId": "L", "remoteCandidateId": "R",
                     "bytesSent": 1000, "currentRoundTripTime": 0.001},
                    {"type": "candidate-pair", "id": "CP2", "timestamp": now, "state": "failed"},
                    {"type": "local-candidate", "id": "L", "candidateType": "host",
                     "protocol": "udp"},
                    {"type": "remote-candidate", "id": "R", "candidateType": "srflx"},
                ],
            }
        ],
    }  # fmt: skip


def test_rows_from_sample():
    rows = rows_by_type(
        stats.rows_from_sample("A", sample(1_000_500, 5000, 10, 0), 1_000_000, "lossy-5", "x")
    )
    inbound = rows["inbound-rtp"]
    assert inbound["t_s"] == 0.5
    assert inbound["side"] == "A" and inbound["kind"] == "video" and inbound["codec"] == "VP8"
    assert inbound["jitter_ms"] == 4.0
    assert inbound["net_step"] == "lossy-5"
    assert rows["outbound-rtp"]["quality_limitation_reason"] == "bandwidth"
    assert rows["remote-inbound-rtp"]["rtt_ms"] == 42.5
    # only the pair the transport actually uses, with its candidates spelled out
    pair = rows["candidate-pair"]
    assert pair["id"] == "CP1"
    assert (pair["local_candidate_type"], pair["remote_candidate_type"], pair["protocol"]) == (
        "host", "srflx", "udp",
    )  # fmt: skip


def rows_by_type(rows):
    by_type = {}
    for row in rows:
        assert row["type"] not in by_type, f"two {row['type']} rows"
        by_type[row["type"]] = row
    return by_type


def test_stats_log_derives_rates_and_skips_repeats():
    out = io.StringIO()
    log = stats.StatsLog(out)
    t0 = 1_000_000
    log.add(stats.rows_from_sample("B", sample(t0, 0, 0, 0), t0))
    # 1 s later: 125000 bytes = 1000 kbit/s, 95 received + 5 newly lost = 5% loss
    log.add(stats.rows_from_sample("B", sample(t0 + 1000, 125_000, 95, 5), t0))
    # nothing new since the last tick --> same timestamps --> no rows
    before = log.rows_written
    log.add(stats.rows_from_sample("B", sample(t0 + 1000, 125_000, 95, 5), t0))
    assert log.rows_written == before

    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert list(rows[0]) == stats.COLUMNS
    later = [r for r in rows if r["t_s"] == "1.0"]
    inbound = next(r for r in later if r["type"] == "inbound-rtp")
    assert float(inbound["bitrate_kbps"]) == 1000
    assert float(inbound["packet_rate"]) == 95
    assert float(inbound["interval_loss_pct"]) == 5
    outbound = next(r for r in later if r["type"] == "outbound-rtp")
    assert float(outbound["bitrate_kbps"]) == 1000


def test_derive_ignores_counter_resets():
    row = {"type": "inbound-rtp", "wall_ms": 2000, "bytes_received": 10, "packets_received": 1}
    stats.derive({"type": "inbound-rtp", "wall_ms": 1000, "bytes_received": 5000}, row)
    assert "bitrate_kbps" not in row


def test_hook_js_is_wired():
    # the runner relies on these names existing in the page
    for name in ("window.__rtpscope", "state.collect", "state.states", "getStats"):
        assert name in stats.HOOK_JS
    assert "collect()" in stats.COLLECT_JS and "states()" in stats.STATES_JS


# ---- runner bits that don't need a browser


def test_summarize(tmp_path):
    path = tmp_path / "stats.csv"
    with path.open("w", newline="") as f:
        log = stats.StatsLog(f)
        t0 = 1_000_000
        log.add(stats.rows_from_sample("A", sample(t0, 0, 0, 0), t0))
        log.add(stats.rows_from_sample("A", sample(t0 + 1000, 125_000, 95, 5), t0))
    (line,) = run.summarize(path)
    assert line.split() == [
        "A", "received", "video", "1000", "kbit/s", "avg,", "5.00%", "lost,", "30", "frames",
        "decoded,", "0", "freezes",
    ]  # fmt: skip


def test_chrome_flags():
    flags = browser.chrome_flags("/tmp/profile")
    for needed in (
        "--remote-debugging-pipe",
        "--use-fake-ui-for-media-stream",
        "--disable-features=WebRtcHideLocalIpsWithMdns",
        "--headless=new",
    ):
        assert needed in flags
    assert "--headless=new" not in browser.chrome_flags("/tmp/profile", headless=False)


def test_find_chrome_explains_when_missing(monkeypatch):
    monkeypatch.setattr(browser, "CANDIDATES", ["/nonexistent/chrome"])
    monkeypatch.delenv("RTPSCOPE_CHROME", raising=False)
    with pytest.raises(browser.BrowserError, match="RTPSCOPE_CHROME"):
        browser.find_chrome()


def test_run_rejects_bad_args(tmp_path, capsys):
    assert run.main(["--browsers", "5", "--no-netem", "--out", str(tmp_path)]) == 1
    assert "2-4" in capsys.readouterr().err
    assert run.main(["--scenario", "nope", "--no-netem", "--out", str(tmp_path)]) == 1


# ---- the real thing: call server + 2 chromes, ~10 s. opt in, it needs chrome + aiohttp


def _have_chrome():
    try:
        browser.find_chrome()
    except browser.BrowserError:
        return False
    return True


@pytest.mark.skipif(
    os.environ.get("RTPSCOPE_E2E") != "1"
    or importlib.util.find_spec("aiohttp") is None
    or not _have_chrome(),
    reason="set RTPSCOPE_E2E=1 (needs chrome and aiohttp)",
)
def test_two_browsers_one_csv(tmp_path):
    args = ["--scenario", "baseline", "--duration", "8", "--settle", "0", "--no-netem"]
    code = run.main([*args, "--out", str(tmp_path)])
    assert code == 0
    (run_dir,) = tmp_path.iterdir()
    rows = list(csv.DictReader((run_dir / "stats.csv").open(newline="")))
    received = {(r["side"], r["kind"]) for r in rows if r["type"] == "inbound-rtp"}
    assert {("A", "video"), ("B", "video"), ("A", "audio"), ("B", "audio")} <= received
    assert any(float(r["bitrate_kbps"] or 0) > 0 for r in rows if r["type"] == "inbound-rtp")
    assert json.loads((run_dir / "meta.json").read_text())["rows"] == len(rows)
    assert Path(run_dir / "network.csv").read_text().count("\n") == 2
