# CloudBench

A cloud laboratory: users and AI agents lease real lab hardware, run experiments on it, and watch the physical results live.

The design lives in the [CloudBench API and Backend Design](https://claude.ai/artifact/Tm5Jt2RaFGNeN77xuKuZfh) doc.
Conventions and gotchas for contributors (human or AI) are in `AGENTS.md`; the current state of work is in `HANDOFF.md`.

## Layout

| Path | What it is |
| --- | --- |
| `src/cloudbench/protocol.py` | Descriptors and gateway/agent messages shared by both sides |
| `src/cloudbench/gateway/` | The gateway: public API, leases, runs, files, experiments, video fan-out |
| `src/cloudbench/agent/` | The workbench agent: a small daemon on the lab computer that drives the hardware |
| `experiments/` | Experiment definitions (YAML) and their assets |
| `web/` | Mobile-first web app for running experiments |
| `deploy/` | Docker Compose for the gateway |
| `tests/` | End-to-end tests that run a real gateway and a real agent with mock drivers |

## Development

Requires [uv](https://docs.astral.sh/uv/) and Node 20+.

```bash
uv sync                  # install Python deps (gateway, agent, dev tools)
uv run pytest            # run the test suite
uv run ruff check .      # lint
uv run ruff format .     # format
uv run pyright           # type check
```

### Run locally with mock hardware

Terminal 1, the gateway:

```bash
CLOUDBENCH_AGENT_SECRETS='{"wb_faraday": "dev-secret"}' uv run cloudbench-gateway
```

Terminal 2, a workbench agent with mock drivers (a simulated petri dish on camera):

```bash
CLOUDBENCH_AGENT_SECRET=dev-secret uv run cloudbench-agent --config agent.example.yaml
```

The API docs are at http://localhost:8000/docs.

### Run with real hardware

Copy `agent.example.yaml` to `agent.local.yaml`, set `vibrator.driver: sounddevice` and `camera.driver: opencv`, pick the devices, and point `gateway_url` at the gateway.

```bash
uv run python -m sounddevice   # list audio outputs
CLOUDBENCH_AGENT_SECRET=... uv run cloudbench-agent --config agent.local.yaml
```

## AI agents (MCP)

The gateway serves an MCP endpoint over Streamable HTTP at `/mcp`.
Get a token from `POST /v1/sessions` and configure your MCP client with the header `Authorization: Bearer <token>`, for example:

```bash
TOKEN=$(curl -s -X POST localhost:8000/v1/sessions -H 'content-type: application/json' \
  -d '{"display_name": "My agent"}' | jq -r .token)
claude mcp add --transport http cloudbench http://localhost:8000/mcp --header "Authorization: Bearer $TOKEN"
```

The tools cover discovery, leases, tone generation and upload, running actions and experiments, and `get_camera_frame` to see the result.

## Gateway configuration

All settings are environment variables prefixed with `CLOUDBENCH_`; see `src/cloudbench/gateway/config.py`.
The important ones:

| Variable | Purpose |
| --- | --- |
| `CLOUDBENCH_AGENT_SECRETS` | JSON map of workbench ID to agent secret |
| `CLOUDBENCH_SIGNING_SECRET` | Signs short-lived file download links for agents (random per process if unset) |
| `CLOUDBENCH_ADMIN_TOKEN` | Enables `/admin` endpoints |
| `CLOUDBENCH_DATA_DIR` | SQLite database and uploaded files |
| `CLOUDBENCH_EXPERIMENTS_DIR` | Folder of `*/experiment.yaml` definitions |
| `CLOUDBENCH_WEB_DIR` | Built web app to serve at `/` |
| `CLOUDBENCH_LEASE_MAX_DURATION_S` | Lease length (default 180) |

## Deploying the gateway

The gateway runs on a cloud VM with Docker Compose; Caddy in front of it gets a TLS certificate automatically.

```bash
cp deploy/.env.example deploy/.env    # set the domain and secrets
docker compose -f deploy/docker-compose.yml up -d --build
```

Then run the agent on the lab laptop with `gateway_url: https://<your domain>`.
