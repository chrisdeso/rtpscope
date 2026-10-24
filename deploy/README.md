# Deploying the call server (Hetzner VPS)

One small box runs everything: the call server (signaling only, media goes browser to browser), Caddy for HTTPS, and coturn for STUN/TURN so people on strict networks can still connect.

## 1. Server + DNS
- Hetzner Cloud → smallest **cost-optimized x86** plan, **Ubuntu 24.04** (needs its Python 3.12), add your SSH key.
- Point a DNS **A** record (and **AAAA** if you want IPv6) at the server's IP, e.g. `call.example.com`. Check with `dig +short call.example.com` before going on. Caddy can't get a certificate until it resolves.
  **No domain?** Skip DNS and use `DOMAIN=<ip-with-dashes>.sslip.io` (e.g. `203-0-113-7.sslip.io` for `203.0.113.7`). It resolves to that IP with no setup and gets a real Let's Encrypt cert. `nip.io` works the same way.
- If you attach a **Hetzner Cloud Firewall**, open the same ports as the table below there too.

## 2. Install
```sh
ssh root@<server-ip>
apt-get update && apt-get install -y git
git clone https://github.com/chrisdeso/rtpscope.git /opt/rtpscope   # private repo: use a deploy key or token
cd /opt/rtpscope && DOMAIN=call.example.com deploy/setup.sh
```
`setup.sh` installs the packages, builds the venv, writes `/etc/rtpscope/call.env` (STUN/TURN urls + a random TURN secret), renders the coturn, Caddy and systemd configs from this folder, opens the firewall and checks `/health`. It's safe to re-run: the URLs follow `DOMAIN` each time, only the TURN secret is kept. Pass `SKIP_UFW=1` if SSH isn't on port 22 or you only use the Hetzner firewall.

Then open `https://call.example.com`. The first load can take ~30 s while Caddy gets the cert.

## 3. Check TURN works
Open `https://call.example.com/?room=test&relay=1` on two devices. `relay=1` forces all media through coturn, so if video shows up, TURN works. The room pill says "relay only".

## TURN on the LAN, no VPS (dev / demos)
Phones on Wi-Fi often can't reach a laptop directly: many access points block client-to-client multicast, so the browsers' `.local` addresses don't resolve. A TURN server on the machine running the call server fixes that:
```sh
deploy/dev-turn.sh                  # coturn in docker on this machine's LAN IP, prints the server command
# if this machine runs ufw, let the LAN reach coturn:
sudo ufw allow from 192.168.1.0/24 to any port 3478
sudo ufw allow from 192.168.1.0/24 to any port 49160:49260 proto udp
```
Run the printed command (it needs the LAN cert, see the main README), then open `https://<lan-ip>:8087` on each device. Add `&relay=1` to check TURN is really used. Stop it with `docker stop rtpscope-turn`. Unlike `turnserver.conf`, this allows relaying to private addresses, so keep it on the LAN.

## Updating
```sh
cd /opt/rtpscope && git pull && systemctl restart rtpscope-call
```
Re-run `DOMAIN=… deploy/setup.sh` if anything in `deploy/` or the dependencies changed.

## Moving from sslip.io to a real domain
Point the new domain's A record at the server, wait for `dig +short` to show the IP, then re-run with the new name:
```sh
cd /opt/rtpscope && DOMAIN=call.example.com deploy/setup.sh
```
Caddy, coturn and the STUN/TURN URLs all switch over, and the old sslip.io link stops working.

## Logs
```sh
journalctl -u rtpscope-call -f   # joins / leaves
journalctl -u coturn -f          # turn allocations, 401/403s
journalctl -u caddy -f           # certs, proxy errors
```

## Ports
| port | proto | what |
|---|---|---|
| 22 | tcp | ssh |
| 80 | tcp | Let's Encrypt challenge + redirect to https |
| 443 | tcp/udp | https / http3 (Caddy) |
| 3478 | udp/tcp | STUN + TURN (coturn) |
| 49160–49260 | udp | TURN relay ports (match `turnserver.conf`) |

The call server itself only listens on `127.0.0.1:8087` behind Caddy.

## Files
| file | goes to |
|---|---|
| `Caddyfile` | `/etc/caddy/Caddyfile` |
| `turnserver.conf` | `/etc/turnserver.conf` (mode 640, has the secret) |
| `rtpscope-call.service` | `/etc/systemd/system/` |
| generated (shape in `call.env.example`) | `/etc/rtpscope/call.env` (mode 600): `STUN_URL`, `TURN_URL`, `TURN_SECRET` |

Notes: there's no auth on rooms, and the lobby's "Active meetings" list (`/rooms`) shows every live room and who's in it to anyone who opens the page. Treat the site itself as the thing to keep private. Relayed calls send their media through the VPS and count against the plan's traffic.
