#!/usr/bin/env bash
# dev-turn.sh --> coturn in docker for testing on the lan (no vps needed), e.g. phones on wifi
# usage: deploy/dev-turn.sh [lan-ip]      stop it with: docker stop rtpscope-turn
# prints the line to start the call server against it.
# unlike deploy/turnserver.conf this allows relaying to private (lan) addresses, so lan only.
set -euo pipefail

# default --> the address this machine uses to reach the internet, i.e. its lan ip
IP="${1:-$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')}"
SECRET="${TURN_SECRET:-$(openssl rand -hex 16)}"

docker rm -f rtpscope-turn >/dev/null 2>&1 || true
docker run -d --rm --name rtpscope-turn --network host coturn/coturn \
  -n --listening-ip="$IP" --relay-ip="$IP" --listening-port=3478 \
  --use-auth-secret --static-auth-secret="$SECRET" --realm=rtpscope.lan \
  --min-port=49160 --max-port=49260 --fingerprint --no-tls --log-file=stdout >/dev/null

echo "coturn up on $IP:3478 (relay ports 49160-49260/udp), logs: docker logs -f rtpscope-turn"
echo "if this machine runs ufw, open those ports to the lan (see deploy/README.md)"
echo
echo "start the call server with:"
echo "  TURN_SECRET=$SECRET STUN_URL=stun:$IP:3478 \\"
echo "  TURN_URL='turn:$IP:3478?transport=udp,turn:$IP:3478?transport=tcp' \\"
echo "  .venv/bin/python -m rtpscope.call.server --cert certs/cert.pem --key certs/key.pem"
