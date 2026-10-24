# certs

Self-signed TLS cert for running the call server over HTTPS on the LAN (the camera only works on https or localhost). Everything in here except this file is gitignored, so never commit keys.

```sh
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj /CN=rtpscope \
  -keyout certs/key.pem -out certs/cert.pem
.venv/bin/python -m rtpscope.call.server --cert certs/cert.pem --key certs/key.pem
```
Browsers will warn about the cert, click through once per device. The VPS doesn't need this: Caddy gets a real cert (see `deploy/README.md`).
