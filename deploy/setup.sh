#!/usr/bin/env bash
# setup.sh --> one shot setup for a fresh ubuntu 24.04 vps (call server + caddy + coturn + firewall)
# run as root from the repo clone in /opt/rtpscope:
#   DOMAIN=call.example.com deploy/setup.sh
# safe to re-run (after a git pull etc), it keeps the turn secret it made the first time.
# SKIP_UFW=1 leaves the firewall alone (e.g. ssh isn't on 22, or you use the hetzner cloud firewall)
set -euo pipefail

: "${DOMAIN:?set DOMAIN, e.g. DOMAIN=call.example.com deploy/setup.sh}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$REPO_DIR/deploy"
ENV_FILE=/etc/rtpscope/call.env

if [[ $EUID -ne 0 ]]; then
  echo "run as root (sudo)" >&2
  exit 1
fi
# the service runs as a throwaway user with ProtectHome, it can't read /root or /home
if [[ $REPO_DIR == /root* || $REPO_DIR == /home* ]]; then
  echo "clone the repo somewhere like /opt/rtpscope, not $REPO_DIR" >&2
  exit 1
fi

echo "--> packages"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -yq python3-venv git caddy coturn ufw openssl curl

echo "--> python venv"
python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install -q -e "$REPO_DIR"

echo "--> $ENV_FILE"
mkdir -p "$(dirname "$ENV_FILE")"
if [[ ! -f $ENV_FILE ]]; then
  (
    umask 077
    cat >"$ENV_FILE" <<EOF
STUN_URL=stun:$DOMAIN:3478
TURN_URL=turn:$DOMAIN:3478?transport=udp,turn:$DOMAIN:3478?transport=tcp
TURN_SECRET=$(openssl rand -hex 32)
EOF
  )
fi
TURN_SECRET="$(grep '^TURN_SECRET=' "$ENV_FILE" | cut -d= -f2-)"

echo "--> coturn"
sed -e "s|__DOMAIN__|$DOMAIN|g" -e "s|__TURN_SECRET__|$TURN_SECRET|g" \
  "$DEPLOY/turnserver.conf" >/etc/turnserver.conf
# secret is in there --> only root + coturn can read it
chmod 640 /etc/turnserver.conf
if getent group turnserver >/dev/null; then chgrp turnserver /etc/turnserver.conf; fi
# older coturn packages ship disabled
if [[ -f /etc/default/coturn ]]; then
  sed -i 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn
fi

echo "--> caddy"
sed "s|__DOMAIN__|$DOMAIN|g" "$DEPLOY/Caddyfile" >/etc/caddy/Caddyfile

echo "--> systemd"
sed "s|__REPO_DIR__|$REPO_DIR|g" "$DEPLOY/rtpscope-call.service" \
  >/etc/systemd/system/rtpscope-call.service
systemctl daemon-reload
systemctl enable -q rtpscope-call coturn caddy
systemctl restart rtpscope-call coturn caddy

if [[ ${SKIP_UFW:-} != 1 ]]; then
  echo "--> firewall"
  ufw allow 22/tcp      # ssh, don't lock ourselves out
  ufw allow 80/tcp      # let's encrypt + redirect to https
  ufw allow 443         # https (tcp) + http/3 (udp)
  ufw allow 3478        # stun/turn, udp + tcp
  ufw allow 49160:49260/udp  # turn relay ports, matches turnserver.conf
  ufw --force enable
fi

echo "--> check"
sleep 2
if ! curl -fsS http://127.0.0.1:8087/health; then
  echo "call server isn't answering --> journalctl -u rtpscope-call -n 50" >&2
  exit 1
fi
echo
echo "done --> https://$DOMAIN (first load can take ~30s while caddy gets the cert)"
