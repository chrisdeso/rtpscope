# rtpscope

Diagnose whose fault a degraded WebRTC call is — **wire path** (RTP/RTCP on the network) vs. **pixel path** (what is actually rendered).

CS 4372 Multimedia Systems, Texas State, Fall 2026.

## Workstreams
Wire path · Pixel path · Conferencing app · Correlation + HUD · Test harness · Docs. See `CODEOWNERS`.

## Development
```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pre-commit install   # ruff lint + format on every commit
.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .
```
Editor support: `basedpyright` (completion, types) and `ruff` (lint, format) both ship in the dev extras and speak LSP; `pyproject.toml` points them at `.venv`. Point your editor's LSP client at `.venv/bin/basedpyright-langserver` and `.venv/bin/ruff server`.

## Workflow
- `main` is always demoable; branch as `<area>/<short-desc>` (e.g. `wire/seq-tracker`).
- Conventional Commits (`feat(wire): …`). Open PRs early as drafts, link issues with `Closes #N`.
- 1 approving review, squash merge. CI (`pytest` + `ruff check`) must pass.
- Never commit pcaps, recordings, `runs/`, `.env`, keys, or real IPs (use 192.0.2.x / 198.51.100.x / 203.0.113.x).
