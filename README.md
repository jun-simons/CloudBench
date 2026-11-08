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

### Image analysis on the camera

The agent can measure the wave pattern in the latest camera frame (ported from the CameraTest toolkit). These are
actions on the `camera` tool; they only read the frame, so they can run while the vibrator is playing.
They find the dish by its red colour, so the dish must be visible. The camera and dish must not move between
`capture_reference` and `activity`.

| action | returns |
|---|---|
| `detect_points` | crests (or specular `highlights`): count, density per cm², positions in mm from the dish centre |
| `fourier` | wavelength, k, spectral peaks, and the angular harmonics of the spectral ring (m=4 square, m=6 hexagonal) |
| `delaunay` | triangulation of the points: neighbours per point, bond lengths, bond-orientational order `psi4`/`psi6` |
| `analyze` | dish geometry plus the three above in one call |
| `capture_reference`, `activity` | wave activity against a still reference (about 0.6 is noise, 10+ is strong waves) |

Notes: a perfect square lattice triangulates with one diagonal per cell, so its coordination reads about 6; judge square
versus hexagonal by `psi4` against `psi6` and by the `fourier` symmetry harmonics. Millimetres assume `vision.cup_diameter_mm`.

Set `vision.stream_fps` (default 0, off) to also publish annotated streams `points`, `delaunay` and `spectrum` on the camera
tool, viewable like `video` (for example `get_camera_frame` with `stream: "points"`). Set `vision.enabled: false` to remove all of this.

## AI agents (MCP)

The gateway serves an MCP endpoint over Streamable HTTP at `/mcp`.
No setup is needed beyond the URL: an agent signs in by calling the `start_session` tool and passes the token it returns as `session_token` to every other tool.
If your MCP client can send headers, you can instead get a token from `POST /v1/sessions` and configure the header `Authorization: Bearer <token>`, for example:

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
| `CLOUDBENCH_DEMO_RESET_TOKEN` | Hackathon only: serves a page at `/demo-reset/<token>` whose button ends every session and clears every queue; anyone with the URL can use it |
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
