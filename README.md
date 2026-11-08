# CloudBench

A cloud laboratory: users and AI agents lease real lab hardware, run experiments on it, and watch the physical results live.

The design lives in the [CloudBench API and Backend Design](https://claude.ai/artifact/Tm5Jt2RaFGNeN77xuKuZfh) doc.

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
