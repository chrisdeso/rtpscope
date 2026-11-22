# run.py --> one experiment: call server + N headless browsers in a call + the fake network
# following a scenario, with every browser's webrtc stats going into one csv.
#
#   sudo -v   # once, so tc can run (linux)
#   python -m rtpscope.testbed.run --scenario loss-staircase
#   python -m rtpscope.testbed.run --scenario baseline --no-netem     # mac, or no root
#
# output: runs/<time>-<scenario>/
#   stats.csv    every browser's stats, one row per stream per second (see stats.COLUMNS)
#   network.csv  when each impairment step actually went on
#   meta.json    scenario, settings, versions, row counts
#   server.log, chrome-<side>.log
import argparse
import contextlib
import csv
import json
import platform
import signal
import socket
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from rtpscope.testbed.browser import BrowserError, Chrome
from rtpscope.testbed.netem import (
    DEFAULT_DEV,
    SCENARIOS,
    Netem,
    NetemError,
    Scenario,
    load_scenario,
)
from rtpscope.testbed.stats import COLLECT_JS, HOOK_JS, STATES_JS, StatsLog, rows_from_sample

SIDES = string.ascii_uppercase  # browser names: A, B, C, D


class RunError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout_s: float, proc: subprocess.Popen | None = None) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RunError(f"call server exited with code {proc.returncode}")
        with (
            contextlib.suppress(urllib.error.URLError, OSError),
            urllib.request.urlopen(url, timeout=1) as resp,
        ):
            if resp.status == 200:
                return
        time.sleep(0.2)
    raise RunError(f"call server not answering at {url}")


def start_server(port: int, log_path: Path) -> subprocess.Popen:
    log = log_path.open("wb")
    try:
        # -u --> join/leave lines land in server.log as they happen
        return subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "rtpscope.call.server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# fill the lobby form like a person would --> the call app needs no testbed-only code
JOIN_JS = """
(() => {
  const form = document.getElementById("join-form");
  if (!form) throw new Error("no join form on the page");
  document.getElementById("name").value = %(name)s;
  document.getElementById("room").value = %(room)s;
  document.getElementById("pattern").checked = %(pattern)s;
  form.requestSubmit();
  return true;
})()
"""
READY_JS = "document.readyState === 'complete' && !!document.getElementById('join-form')"
JOIN_ERROR_JS = (
    "(() => { const e = document.getElementById('join-error');"
    " return e && !e.hidden ? e.textContent : ''; })()"
)


def join_call(browser: Chrome, base_url: str, room: str, name: str, pattern: bool) -> None:
    browser.page("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK_JS})
    browser.page("Page.navigate", {"url": f"{base_url}/?{urlencode({'room': room})}"})
    browser.wait_for(READY_JS, 15, "the call page to load")
    params = {"name": json.dumps(name), "room": json.dumps(room), "pattern": json.dumps(pattern)}
    browser.evaluate(JOIN_JS % params)


def wait_connected(browsers: list[Chrome], timeout_s: float) -> None:
    """every browser has a connected RTCPeerConnection to every other one."""
    want = len(browsers) - 1
    deadline = time.monotonic() + timeout_s
    states: dict[str, list[str]] = {}
    while time.monotonic() < deadline:
        for b in browsers:
            error = b.evaluate(JOIN_ERROR_JS)
            if error:
                raise RunError(f"{b.name} couldn't join: {error}")
            states[b.name] = b.evaluate(STATES_JS) or []
        if all(s.count("connected") >= want for s in states.values()):
            return
        time.sleep(0.25)
    detail = "; ".join(f"{name}: {s or 'no peer connection'}" for name, s in states.items())
    console = [f"{b.name} {line}" for b in browsers for line in b.console[-5:]]
    raise RunError(
        f"browsers didn't connect within {timeout_s:g}s ({detail})"
        + ("\n  " + "\n  ".join(console) if console else "")
    )


def settle(browsers: list[Chrome], max_s: float) -> tuple[float | None, list[str]]:
    """wait for every sender to finish its start-up ramp.

    a fresh call starts low (~270p) and climbs for ~20-30 s with qualityLimitationReason
    "bandwidth" even on a perfect network. impairing during the ramp would mix the two up,
    so the scenario clock starts once every outgoing video stream says "none" 3 times in a row.
    returns (seconds it took or None if it never did, the last reasons seen).
    """
    start = time.monotonic()
    calm = 0
    reasons: list[str] = []
    while time.monotonic() - start < max_s:
        reasons = []
        for b in browsers:
            for pc in (b.evaluate(COLLECT_JS) or {}).get("pcs", []):
                reasons += [
                    s.get("qualityLimitationReason", "")
                    for s in pc.get("stats", [])
                    if s.get("type") == "outbound-rtp" and s.get("kind") == "video"
                ]
        calm = calm + 1 if reasons and all(r == "none" for r in reasons) else 0
        if calm >= 3:
            return time.monotonic() - start, reasons
        time.sleep(1)
    return None, reasons


def summarize(stats_path: Path) -> list[str]:
    """per side, per media kind: what came in. enough to eyeball that a run worked."""
    totals: defaultdict[tuple[str, str], defaultdict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    with stats_path.open(newline="") as f:
        for row in csv.DictReader(f):
            if row["type"] != "inbound-rtp":
                continue
            agg = totals[(row["side"], row["kind"])]
            if row["bitrate_kbps"]:
                agg["bitrate_sum"] += float(row["bitrate_kbps"])
                agg["n"] += 1
            for col in ("packets_received", "packets_lost", "frames_decoded", "freeze_count"):
                if row[col]:
                    agg[col] = max(agg[col], float(row[col]))
    lines = []
    for (side, kind), agg in sorted(totals.items()):
        mean = agg["bitrate_sum"] / agg["n"] if agg["n"] else 0
        received, lost = agg["packets_received"], agg["packets_lost"]
        loss = 100 * lost / (received + lost) if received + lost else 0
        line = f"  {side} received {kind:<5} {mean:7.0f} kbit/s avg, {loss:5.2f}% lost"
        if kind == "video":
            line += (
                f", {agg['frames_decoded']:.0f} frames decoded, {agg['freeze_count']:.0f} freezes"
            )
        lines.append(line)
    return lines


def run(args: argparse.Namespace) -> Path:
    scenario: Scenario = load_scenario(args.scenario, args.duration)
    if not 2 <= args.browsers <= 4:
        raise RunError("--browsers has to be 2-4 (the call server allows 4 per room)")

    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{scenario.name}"
    out_dir = Path(args.out) / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    room = args.room or f"testbed-{run_id}"

    # check tc works before starting anything else --> fail in a second, not after a minute
    netem: Netem | None = None
    if not args.no_netem:
        netem = Netem(args.dev, dry_run=args.dry_run_netem)

    server = None
    browsers: list[Chrome] = []
    meta: dict[str, Any] = {
        "run_id": run_id,
        "room": room,
        "started": datetime.now().astimezone().isoformat(),
        "scenario": {
            "name": scenario.name,
            "duration_s": scenario.duration_s,
            "steps": [
                {"at_s": s.at_s, "name": s.name, **asdict(s.impairment)} for s in scenario.steps
            ],
        },
        "netem": "off" if netem is None else ("dry-run" if args.dry_run_netem else args.dev),
        "interval_s": args.interval,
        "browsers": args.browsers,
        "media": "camera (chrome fake device)" if args.camera else "test pattern",
        "chrome_args": args.chrome_arg,
        "platform": platform.platform(),
        "python": platform.python_version(),
    }
    stats_path = out_dir / "stats.csv"
    print(f"run {run_id} --> {out_dir}")
    try:
        if args.url:
            base_url = args.url.rstrip("/")
        else:
            port = args.port or free_port()
            base_url = f"http://127.0.0.1:{port}"
            server = start_server(port, out_dir / "server.log")
        wait_http(f"{base_url}/health", 15, server)
        meta["server"] = base_url

        for i in range(args.browsers):
            side = SIDES[i]
            browser = Chrome(
                side,
                args.chrome,
                headless=not args.show,
                log_path=out_dir / f"chrome-{side}.log",
                extra_args=args.chrome_arg,
            )
            browsers.append(browser)
            browser.start()
        meta["chrome"] = browsers[0].version
        print(f"  {len(browsers)} x {browsers[0].version} joining room {room!r}")

        for browser in browsers:
            join_call(browser, base_url, room, browser.name, pattern=not args.camera)
            # let each join land before the next one --> newcomers offer, nobody races
            time.sleep(0.5)
        wait_connected(browsers, args.connect_timeout)
        print("  connected")
        if args.settle > 0:
            took, reasons = settle(browsers, args.settle)
            meta["settle_s"] = None if took is None else round(took, 1)
            if took is None:
                why = ", ".join(reasons)
                print(f"  still ramping after {args.settle:g}s ({why}), starting anyway")
            else:
                print(f"  video settled after {took:.0f}s, starting the scenario")

        with (
            stats_path.open("w", newline="") as stats_file,
            (out_dir / "network.csv").open("w", newline="") as net_file,
        ):
            log = StatsLog(stats_file)
            net_log = csv.writer(net_file)
            net_log.writerow(
                ["t_s", "wall_ms", "step", "applied", *asdict(scenario.steps[0].impairment)]
            )
            run_loop(scenario, browsers, netem, log, net_log, net_file, args.interval)
            meta["rows"] = log.rows_written
    finally:
        if netem is not None:
            with contextlib.suppress(NetemError):
                netem.clear()
        for browser in browsers:
            browser.close()
        stop_process(server)
        meta["ended"] = datetime.now().astimezone().isoformat()
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    print(f"  {meta.get('rows', 0)} rows --> {stats_path}")
    for line in summarize(stats_path):
        print(line)
    return out_dir


def run_loop(
    scenario: Scenario,
    browsers: list[Chrome],
    netem: Netem | None,
    log: StatsLog,
    net_log: Any,
    net_file: Any,
    interval_s: float,
) -> None:
    t0 = time.time()
    t0_ms = t0 * 1000
    step = None
    tick = 0
    while True:
        t = time.time() - t0
        if t >= scenario.duration_s:
            break
        current = scenario.step_at(t)
        if current is not step:
            step = current
            applied = "no"
            if netem is not None:
                netem.apply(step.impairment)
                applied = "dry-run" if netem.dry_run else "yes"
            now = time.time()
            net_log.writerow(
                [
                    round(now - t0, 3),
                    round(now * 1000, 1),
                    step.name,
                    applied,
                    *asdict(step.impairment).values(),
                ]
            )
            net_file.flush()
            print(f"  {t:6.1f}s  network: {step.name} ({step.impairment.describe()})")
        desc = step.impairment.describe() if netem is not None else "not applied"
        for browser in browsers:
            sample = browser.evaluate(COLLECT_JS, timeout_s=10)
            log.add(rows_from_sample(browser.name, sample or {}, t0_ms, step.name, desc))
        tick += 1
        # fixed grid (t0 + n * interval) --> no drift over a long run
        time.sleep(max(0.0, t0 + tick * interval_s - time.time()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="rtpscope testbed: browsers in a call over a fake network, stats to csv",
        epilog="scenarios: " + ", ".join(SCENARIOS) + " (or any profile name, or a json file)",
    )
    parser.add_argument("--scenario", default="baseline", help="default: baseline")
    parser.add_argument("--duration", type=float, help="seconds, overrides the scenario's")
    parser.add_argument("--interval", type=float, default=1.0, help="stats every N s (default 1)")
    parser.add_argument("--browsers", type=int, default=2, help="2-4 (default 2)")
    parser.add_argument("--out", default="runs", help="output folder (default runs/, gitignored)")
    parser.add_argument("--room", help="room name (default testbed-<run id>)")
    parser.add_argument(
        "--url", help="use a call server that's already running, e.g. http://127.0.0.1:8087"
    )
    parser.add_argument(
        "--port", type=int, help="port for the call server we start (default: free one)"
    )
    parser.add_argument("--dev", default=DEFAULT_DEV, help="interface for netem (default lo)")
    parser.add_argument("--no-netem", action="store_true", help="don't touch the network at all")
    parser.add_argument(
        "--dry-run-netem", action="store_true", help="print tc commands instead of running them"
    )
    parser.add_argument(
        "--camera", action="store_true", help="chrome's fake camera instead of the test pattern"
    )
    parser.add_argument(
        "--chrome", help="chrome/chromium binary (default: search PATH, or RTPSCOPE_CHROME)"
    )
    parser.add_argument(
        "--chrome-arg",
        action="append",
        default=[],
        help="extra chrome flag, repeatable (e.g. --chrome-arg=--force-fieldtrials=...)",
    )
    parser.add_argument(
        "--show", action="store_true", help="show the browser windows (not headless)"
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=45,
        help="max seconds to wait for the video to finish ramping up before the scenario "
        "starts (default 45, 0 = don't wait)",
    )
    parser.add_argument("--connect-timeout", type=float, default=30)
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval has to be > 0")

    # ctrl-c or kill --> still clear netem and close the browsers (run()'s finally does it)
    def stop(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        run(args)
    except KeyboardInterrupt:
        print("stopped (network cleared, browsers closed)", file=sys.stderr)
        return 130
    except (RunError, NetemError, BrowserError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
