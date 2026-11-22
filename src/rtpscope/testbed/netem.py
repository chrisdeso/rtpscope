# netem.py --> the fake network we break on purpose (linux tc + netem)
#
# both browsers run on one machine, so their media goes over loopback. we hang a netem qdisc
# off `lo` and send only UDP into it:
#   root prio qdisc (4 bands) --> bands 1-3 untouched, band 4 = netem
#   filter: udp to port 53 --> band 1 (dns stays fast)
#   filter: any other udp (ipv4 + ipv6) --> band 4 (rtp, rtcp, stun all get impaired)
# tcp (the signaling websocket, http) never hits netem, so a broken network can't break setup.
# loopback is one hop, so every packet in both directions goes through netem once.
#
#   python -m rtpscope.testbed.netem list
#   sudo python -m rtpscope.testbed.netem apply lossy-5 [--dev lo]
#   sudo python -m rtpscope.testbed.netem clear
#   python -m rtpscope.testbed.netem apply lossy-5 --dry-run   # just print the tc commands
import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_DEV = "lo"
NETEM_PARENT = "1:4"
NETEM_HANDLE = "40:"


@dataclass(frozen=True)
class Impairment:
    """one network condition. zeros mean "leave it alone"."""

    delay_ms: float = 0
    jitter_ms: float = 0
    loss_pct: float = 0
    # bandwidth cap, 0 = unlimited
    rate_kbit: int = 0
    # netem queue length in packets (the default 1000 hides congestion when rate is set)
    limit: int = 0
    reorder_pct: float = 0

    def __post_init__(self):
        for field, value in asdict(self).items():
            if value < 0:
                raise ValueError(f"{field} can't be negative ({value})")
        if self.loss_pct > 100 or self.reorder_pct > 100:
            raise ValueError("percentages go up to 100")
        if self.jitter_ms and not self.delay_ms:
            raise ValueError("netem needs delay_ms set to use jitter_ms")
        if self.reorder_pct and not self.delay_ms:
            raise ValueError("netem needs delay_ms set to use reorder_pct")

    def netem_args(self) -> list[str]:
        """the bit after `netem` in a tc command."""
        args: list[str] = []
        if self.delay_ms:
            args += ["delay", f"{_num(self.delay_ms)}ms"]
            if self.jitter_ms:
                # normal distribution looks more like real wifi than the default uniform one
                args += [f"{_num(self.jitter_ms)}ms", "distribution", "normal"]
        if self.loss_pct:
            args += ["loss", f"{_num(self.loss_pct)}%"]
        if self.reorder_pct:
            args += ["reorder", f"{_num(self.reorder_pct)}%"]
        if self.rate_kbit:
            args += ["rate", f"{self.rate_kbit}kbit"]
        if self.limit:
            args += ["limit", str(self.limit)]
        # netem with no options is a pass-through, but say so explicitly
        return args or ["delay", "0ms"]

    def describe(self) -> str:
        parts = []
        if self.delay_ms:
            parts.append(
                f"{_num(self.delay_ms)}ms" + (f"±{_num(self.jitter_ms)}" if self.jitter_ms else "")
            )
        if self.loss_pct:
            parts.append(f"{_num(self.loss_pct)}% loss")
        if self.reorder_pct:
            parts.append(f"{_num(self.reorder_pct)}% reorder")
        if self.rate_kbit:
            parts.append(f"{self.rate_kbit} kbit/s")
        if self.limit:
            parts.append(f"queue {self.limit}")
        return ", ".join(parts) or "clean"


def _num(value: float) -> str:
    # 5.0 --> "5", 2.5 --> "2.5"
    return f"{value:g}"


# named conditions --> what you pass to `apply`, and the building blocks for scenarios
PROFILES: dict[str, Impairment] = {
    "clean": Impairment(),
    "lan": Impairment(delay_ms=2, jitter_ms=1),
    "wan": Impairment(delay_ms=40, jitter_ms=5),
    "lossy-2": Impairment(delay_ms=20, loss_pct=2),
    "lossy-5": Impairment(delay_ms=20, loss_pct=5),
    "lossy-10": Impairment(delay_ms=20, loss_pct=10),
    "laggy": Impairment(delay_ms=150, jitter_ms=10),
    "jittery": Impairment(delay_ms=60, jitter_ms=40),
    "reorder": Impairment(delay_ms=30, reorder_pct=10),
    "cap-2m": Impairment(delay_ms=20, rate_kbit=2000, limit=100),
    "cap-1m": Impairment(delay_ms=20, rate_kbit=1000, limit=50),
    "cap-500k": Impairment(delay_ms=20, rate_kbit=500, limit=30),
    "cap-250k": Impairment(delay_ms=20, rate_kbit=250, limit=20),
    "3g": Impairment(delay_ms=100, jitter_ms=20, loss_pct=1, rate_kbit=750, limit=50),
}


@dataclass(frozen=True)
class Step:
    at_s: float
    name: str
    impairment: Impairment


@dataclass(frozen=True)
class Scenario:
    """a timeline of network conditions. each step holds until the next one starts."""

    name: str
    steps: tuple[Step, ...]
    duration_s: float

    def __post_init__(self):
        if not self.steps:
            raise ValueError(f"scenario {self.name!r} has no steps")
        if self.steps[0].at_s != 0:
            raise ValueError(f"scenario {self.name!r} has to start at 0 s")
        times = [s.at_s for s in self.steps]
        if times != sorted(times) or len(set(times)) != len(times):
            raise ValueError(f"scenario {self.name!r}: step times must strictly increase")
        if self.duration_s <= times[-1]:
            raise ValueError(f"scenario {self.name!r}: duration must be after the last step")

    def step_at(self, t_s: float) -> Step:
        current = self.steps[0]
        for step in self.steps:
            if step.at_s <= t_s:
                current = step
        return current


def _scenario(name: str, plan: Sequence[tuple[float, str]], duration_s: float) -> Scenario:
    steps = tuple(Step(at, profile, PROFILES[profile]) for at, profile in plan)
    return Scenario(name, steps, duration_s)


# built-in experiments. every one starts and ends clean, so you get a baseline and a recovery
SCENARIOS: dict[str, Scenario] = {
    # sanity check: no impairment at all
    "baseline": _scenario("baseline", [(0, "clean")], 30),
    # loss gets worse in steps, then goes away
    "loss-staircase": _scenario(
        "loss-staircase",
        [(0, "clean"), (15, "lossy-2"), (30, "lossy-5"), (45, "lossy-10"), (60, "clean")],
        75,
    ),
    # bandwidth squeezed in steps --> should push the sender's bitrate/resolution down
    "bandwidth-staircase": _scenario(
        "bandwidth-staircase",
        [
            (0, "clean"),
            (15, "cap-2m"),
            (30, "cap-1m"),
            (45, "cap-500k"),
            (60, "cap-250k"),
            (75, "clean"),
        ],
        95,
    ),
    # latency and jitter, no loss --> jitter buffer grows, freezes without packet loss
    "delay-jitter": _scenario(
        "delay-jitter", [(0, "clean"), (15, "laggy"), (35, "jittery"), (55, "clean")], 70
    ),
    # a bit of everything, quick
    "mixed": _scenario(
        "mixed",
        [(0, "clean"), (10, "lossy-5"), (20, "cap-500k"), (30, "jittery"), (40, "clean")],
        50,
    ),
}


def load_scenario(spec: str, duration_s: float | None = None) -> Scenario:
    """a built-in scenario name, a profile name (held for the whole run), or a json file.

    json shape:
      {"name": "my-test", "duration_s": 60,
       "steps": [{"at_s": 0, "profile": "clean"},
                 {"at_s": 20, "loss_pct": 3, "delay_ms": 50}]}
    a step uses either a "profile" name or Impairment fields directly (optionally a "name").
    """
    if spec in SCENARIOS:
        scenario = SCENARIOS[spec]
        if duration_s is not None:
            # shorter than planned --> steps that would start after the end just don't happen
            steps = tuple(s for s in scenario.steps if s.at_s < duration_s)
            scenario = Scenario(scenario.name, steps, duration_s)
        return scenario
    if spec in PROFILES:
        return Scenario(spec, (Step(0, spec, PROFILES[spec]),), duration_s or 30)
    path = Path(spec)
    if not path.is_file():
        known = ", ".join([*SCENARIOS, *PROFILES])
        raise ValueError(f"unknown scenario {spec!r}: not a file, and not one of: {known}")
    data = json.loads(path.read_text())
    steps = []
    for i, raw in enumerate(data["steps"]):
        raw = dict(raw)
        at_s = float(raw.pop("at_s"))
        if "profile" in raw:
            profile = raw.pop("profile")
            if profile not in PROFILES:
                raise ValueError(f"{path}: step {i} uses unknown profile {profile!r}")
            if raw:
                raise ValueError(f"{path}: step {i} mixes a profile with {sorted(raw)}")
            steps.append(Step(at_s, profile, PROFILES[profile]))
        else:
            name = raw.pop("name", f"step-{i}")
            steps.append(Step(at_s, name, Impairment(**raw)))
    return Scenario(
        data.get("name", path.stem), tuple(steps), float(duration_s or data["duration_s"])
    )


# ---- tc commands


def setup_commands(dev: str, impairment: Impairment) -> list[list[str]]:
    netem = ["tc", "qdisc", "add", "dev", dev, "parent", NETEM_PARENT, "handle", NETEM_HANDLE]
    filt = ["tc", "filter", "add", "dev", dev, "parent", "1:"]
    return [
        # 4 bands, and the default priomap only ever uses the first 3 --> band 4 gets only
        # what the filters below put there
        ["tc", "qdisc", "add", "dev", dev, "root", "handle", "1:", "prio", "bands", "4"],
        [*netem, "netem", *impairment.netem_args()],
        # dns over udp stays fast (systemd-resolved lives on loopback)
        [*filt, "protocol", "ip", "prio", "1", "u32",
         "match", "ip", "protocol", "17", "0xff", "match", "ip", "dport", "53", "0xffff",
         "flowid", "1:1"],
        [*filt, "protocol", "ip", "prio", "2", "u32",
         "match", "ip", "protocol", "17", "0xff", "flowid", NETEM_PARENT],
        [*filt, "protocol", "ipv6", "prio", "3", "u32",
         "match", "ip6", "protocol", "17", "0xff", "flowid", NETEM_PARENT],
    ]  # fmt: skip


# "unlimited" for a change. the kernel only touches netem's rate when the change carries one,
# so going from a capped step to an uncapped one has to say a rate out loud or the cap sticks
UNCAPPED = "10gbit"


def change_command(dev: str, impairment: Impairment) -> list[str]:
    # change in place --> the filters stay, no gap where packets skip netem, nothing dropped
    args = impairment.netem_args()
    if not impairment.rate_kbit:
        args = [*args, "rate", UNCAPPED]
    return [
        "tc", "qdisc", "change", "dev", dev, "parent", NETEM_PARENT, "handle", NETEM_HANDLE,
        "netem", *args,
    ]  # fmt: skip


def clear_command(dev: str) -> list[str]:
    return ["tc", "qdisc", "del", "dev", dev, "root"]


Runner = Callable[[list[str]], subprocess.CompletedProcess]


class NetemError(RuntimeError):
    pass


class Netem:
    """applies and changes the impairment on one interface. always call clear() when done."""

    def __init__(self, dev: str = DEFAULT_DEV, dry_run: bool = False, runner: Runner | None = None):
        self.dev = dev
        self.dry_run = dry_run
        self.active = False
        self.history: list[list[str]] = []
        self._runner = runner
        self._prefix: list[str] = []
        if not dry_run and runner is None:
            self._prefix = _privilege_prefix()

    def _run(self, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
        self.history.append(cmd)
        if self.dry_run:
            print(" ".join(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        runner = self._runner or _default_runner
        result = runner([*self._prefix, *cmd])
        if check and result.returncode != 0:
            raise NetemError(f"`{' '.join(cmd)}` failed: {(result.stderr or '').strip()}")
        return result

    def apply(self, impairment: Impairment) -> None:
        if self.active:
            self._run(change_command(self.dev, impairment))
            return
        # leftovers from a crashed run would make `add` fail --> clear first, ignore errors
        self._run(clear_command(self.dev), check=False)
        try:
            for cmd in setup_commands(self.dev, impairment):
                self._run(cmd)
        except NetemError:
            self._run(clear_command(self.dev), check=False)
            raise
        self.active = True

    def clear(self) -> None:
        self._run(clear_command(self.dev), check=False)
        self.active = False

    def show(self) -> str:
        if self.dry_run:
            return ""
        result = self._run(["tc", "qdisc", "show", "dev", self.dev], check=False)
        return result.stdout or ""


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _privilege_prefix() -> list[str]:
    if not sys.platform.startswith("linux"):
        raise NetemError(
            "the fake network uses linux tc/netem. on mac or windows run inside a linux vm "
            "(or wsl2), or use --scenario baseline / --no-netem for a clean run"
        )
    if shutil.which("tc") is None:
        raise NetemError("`tc` not found. install it: sudo apt-get install iproute2")
    if os.geteuid() == 0:
        return []
    # not root --> sudo, non-interactive so a password prompt can't hang a run mid-experiment
    if shutil.which("sudo") is None:
        raise NetemError("tc needs root and there's no sudo. run as root")
    probe = subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False)
    if probe.returncode != 0:
        raise NetemError("tc needs root. run `sudo -v` first (caches your password), then retry")
    return ["sudo", "-n"]


# ---- cli


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rtpscope fake network (tc/netem on loopback)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show profiles and scenarios")
    apply = sub.add_parser("apply", help="hold one profile until `clear`")
    apply.add_argument("profile", choices=sorted(PROFILES))
    for p in (apply, sub.add_parser("clear", help="remove the impairment")):
        p.add_argument("--dev", default=DEFAULT_DEV)
        p.add_argument("--dry-run", action="store_true", help="print tc commands, run nothing")
    show = sub.add_parser("show", help="what tc has on the interface now")
    show.add_argument("--dev", default=DEFAULT_DEV)
    args = parser.parse_args(argv)

    if args.cmd == "list":
        print("profiles:")
        for name, imp in PROFILES.items():
            print(f"  {name:<12} {imp.describe()}")
        print("scenarios:")
        for name, sc in SCENARIOS.items():
            plan = " -> ".join(f"{s.name}@{_num(s.at_s)}s" for s in sc.steps)
            print(f"  {name:<20} {_num(sc.duration_s)}s  {plan}")
        return 0
    try:
        if args.cmd == "show":
            print(Netem(args.dev).show(), end="")
            return 0
        netem = Netem(args.dev, dry_run=args.dry_run)
        if args.cmd == "apply":
            netem.apply(PROFILES[args.profile])
            print(f"{args.dev}: {args.profile} ({PROFILES[args.profile].describe()}), udp only")
        else:
            netem.clear()
            print(f"{args.dev}: cleared")
    except NetemError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
