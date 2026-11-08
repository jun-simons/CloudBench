# Handoff

State at the end of 2026-11-08.

## Where things are

| What | State |
| --- | --- |
| `main` | v1 (PR #1 merged) |
| `backend-core` | GitHub's default branch; v1 plus Jeremy's description copy |
| `faraday-frontend` | Two-pane frontend with live vibrator control and presets; [PR #2](https://github.com/jun-simons/CloudBench/pull/2) into `backend-core`, CI green, ready to merge |
| https://cloudbench.work | VM runs `faraday-frontend` at `e9583ea`, one deploy behind the branch (missing Jeremy's copy and the "agent needs updating" hint) |
| Lab laptop agent | Still the old (0.1.0) agent: camera works, but the vibrator switch stays disabled until it is updated |

## First thing tomorrow

1. Update the agent on the lab laptop, in its CloudBench folder:
   ```bash
   git fetch && git checkout faraday-frontend && git pull
   uv sync --no-dev --extra agent
   CLOUDBENCH_AGENT_SECRET=<secret> uv run cloudbench-agent --config agent.local.yaml
   ```
   The gateway log should then show `agent 0.2.0`, and the vibrator switch turns on once you hold the bench.
2. Redeploy the VM to pick up the latest branch (this ends any active session):
   ```bash
   ssh hackrpi@104.238.131.98 'cd ~/CloudBench && git pull && docker compose -f deploy/docker-compose.yml up -d --build'
   ```
3. Test live control on the real exciter at low amplitude first; square and sawtooth drive it much harder than sine at the same setting.

## Open items

- [ ] Replace the placeholder preset values in `experiments/faraday-waves/experiment.yaml` (25/40/80 Hz sine, 50 Hz square) with settings that look good on the real dish, and write their labels.
- [ ] Remaining `TODO:text` copy: app strings in `web/src/copy.ts`, experiment notes and preset names in the experiment file.
- [ ] Merge PR #2, then make `main` the default branch on GitHub and point the VM at it (`git checkout main` in `~/CloudBench`).
- [ ] `www.cloudbench.work` still points at Porkbun's parking page; point it at `104.238.131.98` (or CNAME to `cloudbench.work`) and add it to `CLOUDBENCH_DOMAIN` in `deploy/.env` on the VM.
- [ ] Rotate the VM's sudo password; it was shared in a chat session.
- [ ] Optional: a `cloudbench-agent --list-devices` command to pick the camera and audio output on the laptop without guessing indices.
- [ ] Optional: move the Playwright UI checks (desktop and iPhone flows) into the repo and CI; today they only exist as local scripts.

## VM reference

- SSH: `hackrpi@104.238.131.98`; the repo is at `~/CloudBench` with a GitHub deploy key.
- Stack: `docker compose -f deploy/docker-compose.yml` (gateway + Caddy); Caddy serves `cloudbench.work` and `104-238-131-98.sslip.io` with Let's Encrypt certificates.
- Secrets: `~/CloudBench/deploy/.env` (agent secret, admin token); readable only by `hackrpi`.
- Logs: `docker logs deploy-gateway-1`, `docker logs deploy-caddy-1`.
- Firewall: `ufw` allows 22, 80 and 443.
