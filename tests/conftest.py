"""End-to-end fixtures: a real gateway (uvicorn on a free port) and a real agent
with mock drivers, talking over real WebSockets and HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import shutil
import socket
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import uvicorn

from cloudbench.agent.__main__ import build_agent
from cloudbench.agent.config import AgentConfig, CameraConfig, VibratorConfig
from cloudbench.agent.runtime import Agent
from cloudbench.gateway.app import create_app
from cloudbench.gateway.config import GatewaySettings

REPO = Path(__file__).resolve().parent.parent
WORKBENCH = "wb_faraday"
SECRET = "test-agent-secret"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_wav(duration_s: float = 0.3, frequency_hz: float = 40, rate: int = 48_000) -> bytes:
    t = np.arange(int(duration_s * rate)) / rate
    pcm = (np.sin(2 * np.pi * frequency_hz * t) * 0.5 * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


async def eventually(
    check: Callable[[], Awaitable[Any]], within: float = 5.0, interval: float = 0.05
) -> Any:
    """Poll ``check`` until it returns a truthy value."""
    deadline = asyncio.get_running_loop().time() + within
    while True:
        result = await check()
        if result:
            return result
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"condition not met within {within}s (last: {result!r})")
        await asyncio.sleep(interval)


@dataclass
class EventLog:
    events: list[tuple[str, dict[str, Any]]]

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [data for t, data in self.events if t == event_type]


@contextlib.asynccontextmanager
async def listening(client: httpx.AsyncClient, token: str) -> AsyncIterator[EventLog]:
    """Subscribe to /v1/events and collect events; returns once the stream is live."""
    log = EventLog(events=[])
    ready = asyncio.Event()

    async def listen() -> None:
        async with client.stream("GET", f"/v1/events?access_token={token}") as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            event_type = None
            async for line in response.aiter_lines():
                if line == ": connected":
                    ready.set()
                elif line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: ") and event_type:
                    log.events.append((event_type, json.loads(line[6:])))

    task = asyncio.create_task(listen())
    try:
        await asyncio.wait_for(ready.wait(), 10)
        yield log
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@dataclass
class Lab:
    url: str
    client: httpx.AsyncClient
    agent: Agent | None
    agent_task: asyncio.Task[None] | None

    async def guest(self, name: str = "Judge") -> httpx.AsyncClient:
        """A client with a fresh guest session; its token is in the Authorization header."""
        r = await self.client.post("/v1/sessions", json={"display_name": name})
        assert r.status_code == 201, r.text
        token = r.json()["token"]
        return httpx.AsyncClient(
            base_url=self.url, headers={"Authorization": f"Bearer {token}"}, timeout=10
        )

    async def start_agent(self) -> None:
        config = AgentConfig(
            gateway_url=self.url,
            workbench_id=WORKBENCH,
            name="Test bench",
            vibrator=VibratorConfig(driver="mock", max_duration_s=20),
            camera=CameraConfig(driver="mock", width=160, height=120, fps=15),
        )
        self.agent = build_agent(config, SECRET)
        self.agent_task = asyncio.create_task(self.agent.run())
        await asyncio.wait_for(self.agent.connected.wait(), 10)
        await eventually(self.workbench_online)

    async def stop_agent(self) -> None:
        if self.agent_task is not None:
            self.agent_task.cancel()
            await asyncio.gather(self.agent_task, return_exceptions=True)
            self.agent_task = None

    async def workbench_online(self) -> bool:
        r = await self.client.get(f"/v1/workbenches/{WORKBENCH}")
        return r.status_code == 200 and r.json()["status"] == "online"


@contextlib.asynccontextmanager
async def running_gateway(tmp_path: Path, **overrides: Any) -> AsyncIterator[str]:
    port = free_port()
    # The real experiments plus test-only fixtures.
    experiments = tmp_path / "experiments"
    shutil.copytree(REPO / "experiments", experiments)
    shutil.copytree(REPO / "tests" / "fixtures" / "experiments", experiments, dirs_exist_ok=True)
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": port,
        "data_dir": tmp_path / "data",
        "experiments_dir": experiments,
        "agent_secrets": {WORKBENCH: SECRET},
        "admin_token": "test-admin-token",
        "tick_interval_s": 0.05,
        "lease_offer_timeout_s": 30,
        "lease_max_duration_s": 60,
        "reset_timeout_s": 2,
        "agent_heartbeat_interval_s": 0.5,
    }
    values.update(overrides)
    settings = GatewaySettings(**values)
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


@pytest.fixture
def gateway_overrides() -> dict[str, Any]:
    return {}


@pytest.fixture
async def lab(tmp_path: Path, gateway_overrides: dict[str, Any]) -> AsyncIterator[Lab]:
    async with running_gateway(tmp_path, **gateway_overrides) as url:
        client = httpx.AsyncClient(base_url=url, timeout=10)
        lab = Lab(url=url, client=client, agent=None, agent_task=None)
        await lab.start_agent()
        try:
            yield lab
        finally:
            await lab.stop_agent()
            await client.aclose()
