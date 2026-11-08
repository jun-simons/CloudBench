# Notes for agents working on CloudBench

Read `README.md` for setup and `HANDOFF.md` for the current state of work.
The original design is in the [API and Backend Design doc](https://claude.ai/artifact/Tm5Jt2RaFGNeN77xuKuZfh); the code has moved past it in places (live tone control, presets, MCP).

## Architecture in one paragraph

The gateway (`src/cloudbench/gateway/`, FastAPI + SQLite) owns all state: sessions, leases, runs, files, experiments.
Each workbench runs an agent (`src/cloudbench/agent/`) that dials out to the gateway over a WebSocket and only drives hardware.
`src/cloudbench/protocol.py` holds the descriptors and messages both sides share; change it with both sides in mind.
`src/cloudbench/signals.py` holds the waveforms shared by the agent's live synth and the gateway's WAV generator.
The web app (`web/`, React + Vite) and AI agents (MCP at `/mcp`) are both plain clients of the gateway.

## Commands

```bash
uv sync                      # Python deps
uv run pytest                # end-to-end tests: real gateway + real agent with mock drivers
uv run ruff check . && uv run ruff format --check . && uv run pyright
cd web && npm ci && npm run lint && npm run build   # Node 22 (see .nvmrc)
```

All of these must pass before committing; CI (`.github/workflows/ci.yml`) runs them plus a Docker build and smoke test.
When committing with a pipe such as `pytest | tail`, use `set -o pipefail`, or a failing test can slip through.

To see the app locally with a simulated bench, run the gateway and an agent with `agent.example.yaml` as in the README, then open http://localhost:8000.

## Conventions

- Every user-facing string in the web app lives in `web/src/copy.ts`; experiment-specific text lives in the experiment YAML.
  Descriptive copy that nobody has written yet is a `TODO:text - <what it should say>` placeholder; do not invent final copy.
- The web UI is driven by the experiment file (`parameters` JSON Schemas, `live`, `presets`, `notes`), not by hard-coded action or parameter names.
  Keep it that way so other experiments can reuse it.
- Notes link to presets with `[label](preset:<id>)`; an empty label uses the preset's own label.
  The gateway rejects notes that link to unknown presets.
- Actions that adjust or stop something already running are marked `concurrent: true`; everything else is refused with `tool_busy` while the tool is busy.
- The agent enforces its own hardware limits (`vibrator.*` in the agent config) even though the gateway validates against the same schema.
- Any change to what users see should be checked in a browser at phone width (iPhone 14) and at 1440x900; Playwright works well for this.
- Do not put secrets in the repo: deployment secrets live in `deploy/.env` on the VM, agent secrets in `CLOUDBENCH_AGENT_SECRET` on the lab laptop.

## Gotchas

- The database schema is created with `create_all`, with no migrations.
  Adding a column will not change an existing database (the VM has one in the `deploy_gateway-data` volume); add a migration step or reset the volume deliberately.
- Restarting the gateway revokes every active lease (`gateway_restarted`), so deploys interrupt whoever holds the bench.
- The gateway and the lab laptop's agent must be updated together when actions change; an older agent no longer matches the experiment.
  The gateway log line `Agent wb_faraday online (agent X.Y.Z)` shows which version is connected; bump `__version__` in `src/cloudbench/__init__.py` when the agent changes.
- Each live `set_tone` update is stored as a Run, so a user dragging a slider adds many rows to their run history.
