# rtpscope

Diagnose whose fault a degraded WebRTC call is: the **wire path** (RTP/RTCP on the network) or the **pixel path** (what actually gets rendered).

CS 4372 Multimedia Systems, Texas State, Fall 2026. Owners per area are in `.github/CODEOWNERS`; who did what goes in `CONTRIBUTIONS.md`.

## Status
| area | code | state |
|---|---|---|
| Call (conferencing app) | `src/rtpscope/call/` | v1 works: browser mesh call, up to 4 per room, STUN/TURN, VPS deploy |
| Adaptation | none yet | not started |
| Buffering + sync | none yet | not started |
| Wire path | none yet | not started |
| Pixel path | none yet | not started |
| Correlation + HUD | none yet | not started |
| Test harness | `src/rtpscope/testbed/` | v1 works: fake network (netem), browsers in a call, both sides' stats in one CSV |

## Quickstart
Python 3.11+ (CI and the VPS use 3.12).
```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pre-commit install                 # ruff lint + format on every commit
.venv/bin/python -m rtpscope.call.server     # http://localhost:8087, two tabs, same room
```
`pyproject.toml` lists what the code imports today (aiohttp plus dev tools). `requirements.txt` is the planned full stack (aiortc, av, opencv, scapy and so on). Install it with `.venv/bin/pip install -r requirements.txt` when your area needs it, and move a package into `pyproject.toml` once code imports it.

## Commands
| what | command |
|---|---|
| tests | `.venv/bin/pytest -q` |
| lint / format | `.venv/bin/ruff check . && .venv/bin/ruff format --check .` |
| types | `.venv/bin/basedpyright` |
| everything CI runs | `.venv/bin/pre-commit run --all-files && .venv/bin/pytest -q` |
| call server | `.venv/bin/python -m rtpscope.call.server [--host 0.0.0.0] [--port 8087] [--cert pem --key pem]` |
| experiment | `.venv/bin/python -m rtpscope.testbed.run --scenario loss-staircase` (see Testbed) |
| fault table + plots | `.venv/bin/python -m rtpscope.testbed.analyze --latest` (or a run folder) |
| browser e2e test | `RTPSCOPE_E2E=1 .venv/bin/pytest -q tests/test_testbed.py` (needs Chrome) |

Editor LSP: `.venv/bin/basedpyright-langserver` and `.venv/bin/ruff server`. `pyproject.toml` already points them at `.venv`.

## Layout
```
src/rtpscope/call/server.py     signaling server (aiohttp): rooms, websocket relay, turn logins
src/rtpscope/call/static/       browser client: index.html, call.js (webrtc), style.css
src/rtpscope/testbed/           fake network (netem.py), stats logger (stats.py), chrome driver, runner
tests/                          pytest, test_call.py covers the server
deploy/                         vps install (setup.sh, caddy, coturn, systemd), dev-turn.sh, deploy/README.md
certs/                          local tls certs, gitignored
docs/meetings/                  meeting notes and decisions (ADRs)
```

## Call app
Media goes browser to browser (mesh). The server only relays SDP and ICE candidates and hands out STUN/TURN settings.

- **No camera?** Tick "Use a test pattern". It's also used automatically when the camera fails.
- **Another machine on the LAN:** the camera needs HTTPS (or localhost). Make a throwaway cert (replace `<lan-ip>`), then open `https://<lan-ip>:8087` and click through the warning once per device:
  ```sh
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj /CN=rtpscope \
    -addext "subjectAltName=IP:<lan-ip>,DNS:localhost" -keyout certs/key.pem -out certs/cert.pem
  .venv/bin/python -m rtpscope.call.server --cert certs/cert.pem --key certs/key.pem
  ```
- **Phones on Wi-Fi don't connect:** run a local TURN server with `deploy/dev-turn.sh`. See "TURN on the LAN" in `deploy/README.md`.
- **Hosted (HTTPS, STUN/TURN on a VPS):** see `deploy/README.md`.
- **Check that TURN works:** add `?relay=1` to the URL. That forces all media through TURN.

### Config
| env | flag | meaning |
|---|---|---|
| `STUN_URL` | `--stun` (repeatable) | comma-separated STUN urls, e.g. `stun:call.example.com:3478` |
| `TURN_URL` | `--turn` (repeatable) | comma-separated TURN urls, needs `TURN_SECRET` |
| `TURN_SECRET` | env only | coturn `static-auth-secret`; logins are HMAC'd per join and expire after 12 h |

Shape of the file: `deploy/call.env.example`. With no STUN/TURN set, only same-network calls work.

### HTTP + websocket
| route | returns |
|---|---|
| `GET /` | the call page |
| `GET /health` | plain text, 200 when up |
| `GET /rooms` | `[{room, peers: [names], max, started}]`, the lobby's "Active meetings" (public, there's no auth) |
| `GET /ws` | signaling websocket, JSON messages below |

Client to server: `join{room, name}` (must be first), then `signal{to, data}`.
Server to client: `welcome{id, peers, iceServers}`, `peer-joined{id, name}`, `signal{from, data}`, `peer-left{id}`, `error{reason}`.
The newcomer sends the offers and everyone else only answers, so there's no glare. Mute and camera-off use `track.enabled`, so nothing renegotiates.

## Testbed
One command runs an experiment: it starts the call server, puts 2 headless Chromes in a call (test pattern + fake mic), breaks the network on a schedule, and writes every browser's WebRTC stats to one CSV. Standard library only, nothing extra to install. Needs Chrome or Chromium (`--chrome` or `RTPSCOPE_CHROME` if it isn't on PATH).
```sh
sudo -v                                                        # tc needs root, this caches your password
.venv/bin/python -m rtpscope.testbed.run --scenario loss-staircase
.venv/bin/python -m rtpscope.testbed.run --scenario baseline --no-netem   # mac, or no root
.venv/bin/python -m rtpscope.testbed.netem list                # profiles + scenarios
```
Output goes to `runs/<time>-<scenario>/` (gitignored):

| file | what |
|---|---|
| `stats.csv` | one row per browser (`side` A/B), stream and second. `type` is outbound-rtp, inbound-rtp, remote-inbound-rtp, remote-outbound-rtp or candidate-pair. Has bytes, packets, loss, jitter, RTT, fps, resolution, freezes, NACK/PLI, `quality_limitation_reason`, plus derived `bitrate_kbps`, `interval_loss_pct`, `jitter_buffer_ms`, and `net_step`/`net_desc` (what the fake network was doing then). `t_s` is on one clock for every browser. |
| `network.csv` | when each impairment step went on |
| `meta.json` | scenario, settings, Chrome version, row count |

Before the scenario clock starts, the runner waits (up to `--settle 45` s) for the video to finish ramping up. A new call starts around 270p and climbs for ~25 s while reporting `qualityLimitationReason=bandwidth` on a perfect network, and impairing during that ramp would mix the two up. `t_s = 0` is when the scenario starts.

**Whose fault was it** (`analyze.py`, needs `pip install -e '.[analysis]'`):
```sh
.venv/bin/python -m rtpscope.testbed.analyze --latest      # or runs/<run id>
```
It adds these files to the run folder:

| file | what |
|---|---|
| `faults.csv` / `faults.md` | one row per quality drop on a video stream (`A->B` = A sends, B receives): when, what got worse (`fps 30->12, 720p->360p, 2 freeze(s)`), **blame** (network loss / network bandwidth / network delay-jitter / sender CPU / sender source / receiver), the numbers behind it, and whether that matches what netem was doing. The `.md` is ready to paste on a slide. |
| `timeline.png` | small multiples over time (bitrate, fps, resolution, loss, RTT, jitter buffer), with netem steps shaded and the drops labeled with their blame |
| `summary.csv` | medians per network step per stream, for a results table |
| `timeline.csv` | the joined per-second data the above come from (receiver + matching sender row) |

A drop is a second where received fps < 70% of the clean baseline, resolution goes down, bitrate falls below half, or a freeze starts, compared against the clean lead-in. Each second gets a cause from the same second and the one before, and a new drop starts when the cause changes. When several causes show up together, the network one wins (loss, then bandwidth, then delay), because it also explains the side effects. Thresholds are in `THRESHOLDS` at the top of `analyze.py`. Every verdict prints its evidence, so it can be checked by hand.

**The fake network** is Linux `tc netem` on loopback (both browsers run on one machine). Only UDP goes through it, so media, RTCP and STUN get impaired, but the signaling websocket (TCP) and DNS never do. Each packet goes through netem once per direction, so `delay_ms` is one way (RTT = 2x). It also hits any other localhost UDP on the machine during a run. Built-in scenarios start and end clean: `baseline`, `loss-staircase`, `bandwidth-staircase`, `delay-jitter`, `mixed`. `--scenario` also takes one profile name (held the whole run) or a JSON file:
```json
{"name": "burst", "duration_s": 60,
 "steps": [{"at_s": 0, "profile": "clean"},
           {"at_s": 20, "delay_ms": 50, "loss_pct": 8, "rate_kbit": 800},
           {"at_s": 40, "profile": "clean"}]}
```
On macOS, run `--no-netem` (clean runs only) or use a Linux VM. Windows isn't supported by the runner. `--chrome-arg=<flag>` passes extra flags to Chrome (repeatable). `--dry-run-netem` prints the `tc` commands without running them. If a run gets killed hard, `sudo .venv/bin/python -m rtpscope.testbed.netem clear` removes the impairment.

The call app has no testbed code in it. The runner injects a small script before the page loads that records each `RTCPeerConnection` and polls `getStats()`, then joins through the lobby form like a person would.

## Workflow and rules
- `main` is always demoable and protected. Branch as `<area>/<short-desc>`, open draft PRs early, `Closes #N`.
- Conventional Commits for PR titles (`feat(call): …`). PRs are squash merged, need 1 approving review, and CI (ruff + pytest) must pass.
- Log your work in `CONTRIBUTIONS.md`.
- **Never commit** pcaps, recordings, `runs/`, `.env` or `*.env`, keys or certs, or real IPs (use 192.0.2.x / 198.51.100.x / 203.0.113.x). `.gitignore` covers most of these.
- AI assistants: read this file, `CONTRIBUTING.md` and the module you're touching. Run the "everything CI runs" command before you call something done.
