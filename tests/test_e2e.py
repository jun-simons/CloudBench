"""End-to-end behavior of the gateway with a real (mock-driver) workbench agent."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from cloudbench.protocol import AGENT_CONTROL_PATH
from tests.conftest import WORKBENCH, Lab, eventually, make_wav

PLAY = f"/v1/workbenches/{WORKBENCH}/tools/vibrator/actions/play_waveform/runs"


async def upload_wav(client: httpx.AsyncClient, duration_s: float = 0.3) -> str:
    r = await client.post(
        "/v1/files", files={"file": ("tone.wav", make_wav(duration_s), "audio/wav")}
    )
    assert r.status_code == 201, r.text
    assert r.json()["media_type"] == "audio/wav"
    return r.json()["id"]


async def lease(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.post(f"/v1/workbenches/{WORKBENCH}/leases")
    assert r.status_code in (200, 201), r.text
    return r.json()


async def get_run(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    r = await client.get(f"/v1/runs/{run_id}")
    assert r.status_code == 200
    return r.json()


async def run_finished(client: httpx.AsyncClient, run_id: str) -> dict[str, Any] | None:
    run = await get_run(client, run_id)
    return run if run["state"] in ("succeeded", "failed", "cancelled") else None


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


async def test_workbench_registers_with_tools(lab: Lab) -> None:
    r = await lab.client.get("/v1/workbenches")
    [bench] = r.json()
    assert bench["id"] == WORKBENCH
    assert bench["status"] == "online"
    tools = {t["name"]: t for t in bench["tools"]}
    assert set(tools) == {"vibrator", "camera"}
    actions = {a["name"] for a in tools["vibrator"]["actions"]}
    assert actions == {"play_waveform", "stop"}
    assert tools["camera"]["streams"][0]["name"] == "video"

    r = await lab.client.get(f"/v1/workbenches/{WORKBENCH}/tools/vibrator")
    assert r.json()["type"] == "vibration-exciter"


async def test_agent_with_wrong_secret_is_rejected(lab: Lab) -> None:
    ws_url = lab.url.replace("http://", "ws://") + AGENT_CONTROL_PATH
    with pytest.raises((InvalidStatus, ConnectionClosed)):
        async with connect(
            ws_url, additional_headers={"Authorization": "Bearer nope", "X-Workbench-Id": WORKBENCH}
        ) as ws:
            await ws.recv()


async def test_requests_need_a_session(lab: Lab) -> None:
    r = await lab.client.post(f"/v1/workbenches/{WORKBENCH}/leases")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "authentication_required"


# --------------------------------------------------------------------------- #
# Leases
# --------------------------------------------------------------------------- #


async def test_lease_queue_offer_and_accept(lab: Lab) -> None:
    alice, bob = await lab.guest("Alice"), await lab.guest("Bob")

    a = await lease(alice)
    assert a["state"] == "active"
    assert a["expires_at"] is not None

    b = await lease(bob)
    assert b["state"] == "queued"
    assert b["queue_position"] == 1

    # Requesting again returns the same lease instead of queueing twice.
    again = await bob.post(f"/v1/workbenches/{WORKBENCH}/leases")
    assert again.status_code == 200
    assert again.json()["id"] == b["id"]

    bench = (await lab.client.get(f"/v1/workbenches/{WORKBENCH}")).json()
    assert bench["queue_length"] == 1
    assert bench["current_holder"] == "Alice"

    r = await alice.delete(f"/v1/leases/{a['id']}")
    assert r.json()["state"] == "released"

    async def offered() -> bool:
        return (await bob.get(f"/v1/leases/{b['id']}")).json()["state"] == "offered"

    await eventually(offered)

    r = await bob.post(f"/v1/leases/{b['id']}/accept")
    assert r.json()["state"] == "active"

    # Other people cannot see someone else's lease.
    assert (await alice.get(f"/v1/leases/{b['id']}")).status_code == 404


@pytest.mark.parametrize("gateway_overrides", [{"lease_offer_timeout_s": 0.3}])
async def test_unaccepted_offer_expires_and_passes_on(lab: Lab) -> None:
    alice, bob, carol = await lab.guest("Alice"), await lab.guest("Bob"), await lab.guest("Carol")
    a = await lease(alice)
    b = await lease(bob)
    c = await lease(carol)
    assert c["queue_position"] == 2
    await alice.delete(f"/v1/leases/{a['id']}")

    async def state(client: httpx.AsyncClient, lease_id: str) -> str:
        return (await client.get(f"/v1/leases/{lease_id}")).json()["state"]

    async def bob_expired() -> bool:
        return await state(bob, b["id"]) == "expired"

    await eventually(bob_expired)

    async def carol_offered() -> bool:
        return await state(carol, c["id"]) == "offered"

    await eventually(carol_offered)


@pytest.mark.parametrize("gateway_overrides", [{"lease_max_duration_s": 0.5}])
async def test_lease_expires_after_max_duration(lab: Lab) -> None:
    alice = await lab.guest()
    a = await lease(alice)

    async def expired() -> bool:
        r = (await alice.get(f"/v1/leases/{a['id']}")).json()
        return r["state"] == "expired" and r["end_reason"] == "duration_exceeded"

    await eventually(expired)


async def test_renew_only_when_nobody_waits(lab: Lab) -> None:
    alice, bob = await lab.guest("Alice"), await lab.guest("Bob")
    a = await lease(alice)
    r = await alice.post(f"/v1/leases/{a['id']}/renew")
    assert r.status_code == 200
    await lease(bob)
    r = await alice.post(f"/v1/leases/{a['id']}/renew")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "queue_not_empty"


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


async def test_play_waveform_run_succeeds(lab: Lab) -> None:
    alice = await lab.guest()
    l = await lease(alice)  # noqa: E741
    file_id = await upload_wav(alice, duration_s=0.3)

    r = await alice.post(PLAY, json={"lease_id": l["id"], "input": {"audio": file_id}})
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["state"] in ("pending", "running")
    assert run["input"]["gain"] == 0.5  # default applied by the gateway

    done = await eventually(lambda: run_finished(alice, run["id"]))
    assert done["state"] == "succeeded", done
    assert done["output"]["played_s"] == pytest.approx(0.3, abs=0.1)

    mine = (await alice.get("/v1/me/runs")).json()
    assert [r["id"] for r in mine] == [run["id"]]
    files = (await alice.get("/v1/me/files")).json()
    assert [f["id"] for f in files] == [file_id]


async def test_run_requires_active_lease_and_valid_input(lab: Lab) -> None:
    alice, bob = await lab.guest("Alice"), await lab.guest("Bob")
    file_id = await upload_wav(alice)
    a = await lease(alice)
    b = await lease(bob)  # queued

    r = await bob.post(PLAY, json={"lease_id": b["id"], "input": {"audio": file_id}})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "lease_not_active"

    r = await bob.post(PLAY, json={"lease_id": a["id"], "input": {"audio": file_id}})
    assert r.status_code == 403

    r = await alice.post(PLAY, json={"lease_id": a["id"], "input": {"audio": file_id, "gain": 7}})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_input"

    # Bob's file is not visible to Alice, and vice versa.
    bobs_file = await upload_wav(bob)
    r = await alice.post(PLAY, json={"lease_id": a["id"], "input": {"audio": bobs_file}})
    assert r.status_code == 422

    r = await alice.post(
        f"/v1/workbenches/{WORKBENCH}/tools/vibrator/actions/explode/runs",
        json={"lease_id": a["id"]},
    )
    assert r.status_code == 404


async def test_non_wav_upload_claiming_wav_is_rejected(lab: Lab) -> None:
    alice = await lab.guest()
    r = await alice.post("/v1/files", files={"file": ("x.wav", b"not audio at all", "audio/wav")})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_file"


async def test_tool_busy_and_cancel(lab: Lab) -> None:
    alice = await lab.guest()
    a = await lease(alice)
    file_id = await upload_wav(alice, duration_s=5)
    body = {"lease_id": a["id"], "input": {"audio": file_id}}

    first = (await alice.post(PLAY, json=body)).json()
    r = await alice.post(PLAY, json=body)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "tool_busy"

    async def running() -> bool:
        return (await get_run(alice, first["id"]))["state"] == "running"

    await eventually(running)
    await alice.post(f"/v1/runs/{first['id']}/cancel")
    done = await eventually(lambda: run_finished(alice, first["id"]))
    assert done["state"] == "cancelled"


async def test_releasing_lease_stops_running_action_before_next_holder(lab: Lab) -> None:
    alice, bob = await lab.guest("Alice"), await lab.guest("Bob")
    a = await lease(alice)
    b = await lease(bob)
    file_id = await upload_wav(alice, duration_s=5)
    run = (await alice.post(PLAY, json={"lease_id": a["id"], "input": {"audio": file_id}})).json()

    async def running() -> bool:
        return (await get_run(alice, run["id"]))["state"] == "running"

    await eventually(running)
    await alice.delete(f"/v1/leases/{a['id']}")

    done = await eventually(lambda: run_finished(alice, run["id"]))
    assert done["state"] == "cancelled"

    async def offered() -> bool:
        return (await bob.get(f"/v1/leases/{b['id']}")).json()["state"] == "offered"

    await eventually(offered)


async def test_agent_disconnect_revokes_lease_and_fails_run(lab: Lab) -> None:
    alice = await lab.guest()
    a = await lease(alice)
    file_id = await upload_wav(alice, duration_s=5)
    run = (await alice.post(PLAY, json={"lease_id": a["id"], "input": {"audio": file_id}})).json()

    await lab.stop_agent()

    done = await eventually(lambda: run_finished(alice, run["id"]))
    assert done["state"] == "failed"
    assert done["error"]["code"] == "workbench_offline"
    r = (await alice.get(f"/v1/leases/{a['id']}")).json()
    assert r["state"] == "revoked"
    assert r["end_reason"] == "workbench_offline"
    bench = (await lab.client.get(f"/v1/workbenches/{WORKBENCH}")).json()
    assert bench["status"] == "offline"

    # Queueing while offline is allowed; the bench is offered when it comes back.
    q = await lease(alice)
    assert q["state"] == "queued"
    await lab.start_agent()

    async def offered() -> bool:
        return (await alice.get(f"/v1/leases/{q['id']}")).json()["state"] == "offered"

    await eventually(offered)


async def test_admin_maintenance_revokes_and_blocks(lab: Lab) -> None:
    alice = await lab.guest()
    a = await lease(alice)
    admin = {"Authorization": "Bearer test-admin-token"}

    r = await lab.client.post(
        f"/admin/workbenches/{WORKBENCH}/maintenance", json={"maintenance": True}
    )
    assert r.status_code == 403
    r = await lab.client.post(
        f"/admin/workbenches/{WORKBENCH}/maintenance", json={"maintenance": True}, headers=admin
    )
    assert r.status_code == 204

    assert (await alice.get(f"/v1/leases/{a['id']}")).json()["state"] == "revoked"
    r = await alice.post(f"/v1/workbenches/{WORKBENCH}/leases")
    assert r.status_code == 409
    bench = (await lab.client.get(f"/v1/workbenches/{WORKBENCH}")).json()
    assert bench["status"] == "maintenance"


# --------------------------------------------------------------------------- #
# Events and streams
# --------------------------------------------------------------------------- #


async def test_event_stream_pushes_lease_and_run_updates(lab: Lab) -> None:
    alice = await lab.guest()
    token = alice.headers["Authorization"].split()[1]
    events: list[tuple[str, dict[str, Any]]] = []

    async def listen() -> None:
        async with lab.client.stream("GET", f"/v1/events?access_token={token}") as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            event_type = None
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: ") and event_type:
                    events.append((event_type, json.loads(line[6:])))

    listener = asyncio.create_task(listen())
    try:
        await asyncio.sleep(0.2)
        a = await lease(alice)
        file_id = await upload_wav(alice, duration_s=0.2)
        run = (
            await alice.post(PLAY, json={"lease_id": a["id"], "input": {"audio": file_id}})
        ).json()

        async def saw_success() -> bool:
            return any(
                t == "run.updated" and d["id"] == run["id"] and d["state"] == "succeeded"
                for t, d in events
            )

        await eventually(saw_success)
        assert any(t == "lease.updated" and d["state"] == "active" for t, d in events)
        assert any(t == "workbench.updated" for t, _ in events)
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


async def test_mjpeg_stream_and_snapshot(lab: Lab) -> None:
    base = f"/v1/workbenches/{WORKBENCH}/tools/camera/streams/video"

    async def snapshot() -> bytes | None:
        r = await lab.client.get(f"{base}/frame.jpg")
        return r.content if r.status_code == 200 else None

    jpeg = await eventually(snapshot)
    assert jpeg[:2] == b"\xff\xd8"

    frames = 0
    async with lab.client.stream("GET", base) as response:
        assert response.headers["content-type"].startswith("multipart/x-mixed-replace")
        buffer = b""
        async for chunk in response.aiter_bytes():
            buffer += chunk
            frames = buffer.count(b"Content-Type: image/jpeg")
            if frames >= 3:
                break
    assert frames >= 3


# --------------------------------------------------------------------------- #
# Experiments
# --------------------------------------------------------------------------- #


async def test_faraday_experiment_runs_end_to_end(lab: Lab) -> None:
    experiments = (await lab.client.get("/v1/experiments")).json()
    [exp] = [e for e in experiments if e["id"] == "exp_faraday_waves"]
    assert exp["compatible_workbenches"] == [
        {"workbench_id": WORKBENCH, "bindings": {"exciter": "vibrator", "observer": "camera"}}
    ]

    alice = await lab.guest()
    a = await lease(alice)
    r = await alice.post(
        "/v1/experiments/exp_faraday_waves/runs",
        json={"lease_id": a["id"], "parameters": {"frequency_hz": 50, "duration_s": 5}},
    )
    assert r.status_code == 202, r.text
    xrun = r.json()
    assert xrun["parameter_values"] == {"frequency_hz": 50, "gain": 0.4, "duration_s": 5}

    async def finished() -> dict[str, Any] | None:
        x = (await alice.get(f"/v1/experiment-runs/{xrun['id']}")).json()
        return x if x["state"] in ("succeeded", "failed", "cancelled") else None

    done = await eventually(finished, within=15)
    assert done["state"] == "succeeded", done
    [run] = done["runs"]
    assert run["action"] == "play_waveform"
    assert run["input"]["gain"] == 0.4
    assert run["output"]["played_s"] == pytest.approx(5, abs=0.3)

    # The generated waveform landed in the user's library.
    [generated] = (await alice.get("/v1/me/files")).json()
    assert generated["id"] == run["input"]["audio"]
    assert generated["filename"].endswith("50hz.wav")
    assert [x["id"] for x in (await alice.get("/v1/me/experiment-runs")).json()] == [xrun["id"]]


async def test_experiment_rejects_out_of_range_parameters(lab: Lab) -> None:
    alice = await lab.guest()
    a = await lease(alice)
    r = await alice.post(
        "/v1/experiments/exp_faraday_waves/runs",
        json={"lease_id": a["id"], "parameters": {"frequency_hz": 5000}},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_parameters"


async def test_cancelling_experiment_stops_the_vibrator(lab: Lab) -> None:
    alice = await lab.guest()
    a = await lease(alice)
    xrun = (
        await alice.post(
            "/v1/experiments/exp_faraday_waves/runs",
            json={"lease_id": a["id"], "parameters": {"duration_s": 15}},
        )
    ).json()

    async def running() -> bool:
        x = (await alice.get(f"/v1/experiment-runs/{xrun['id']}")).json()
        return bool(x["runs"]) and x["runs"][0]["state"] == "running"

    await eventually(running)
    r = await alice.post(f"/v1/experiment-runs/{xrun['id']}/cancel")
    assert r.json()["state"] == "cancelled"
    [run] = r.json()["runs"]
    done = await eventually(lambda: run_finished(alice, run["id"]))
    assert done["state"] == "cancelled"
