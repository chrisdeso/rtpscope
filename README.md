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
| Test harness | none yet | not started |

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

Editor LSP: `.venv/bin/basedpyright-langserver` and `.venv/bin/ruff server`. `pyproject.toml` already points them at `.venv`.

## Layout
```
src/rtpscope/call/server.py     signaling server (aiohttp): rooms, websocket relay, turn logins
src/rtpscope/call/static/       browser client: index.html, call.js (webrtc), style.css
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

## Workflow and rules
- `main` is always demoable and protected. Branch as `<area>/<short-desc>`, open draft PRs early, `Closes #N`.
- Conventional Commits for PR titles (`feat(call): …`). PRs are squash merged, need 1 approving review, and CI (ruff + pytest) must pass.
- Log your work in `CONTRIBUTIONS.md`.
- **Never commit** pcaps, recordings, `runs/`, `.env` or `*.env`, keys or certs, or real IPs (use 192.0.2.x / 198.51.100.x / 203.0.113.x). `.gitignore` covers most of these.
- AI assistants: read this file, `CONTRIBUTING.md` and the module you're touching. Run the "everything CI runs" command before you call something done.
