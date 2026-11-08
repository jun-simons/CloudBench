"""The workbench agent runtime: dials the gateway, executes runs, streams video.

Safety rule: whenever the agent loses its connection to the gateway, or a lease
ends, every tool is reset to its safe state. Hardware never keeps running
without a gateway that knows about it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from cloudbench import __version__
from cloudbench.agent.camera import FrameStream
from cloudbench.agent.tools import ActionError, FileFetcher, Tool
from cloudbench.protocol import (
    AGENT_CONTROL_PATH,
    AGENT_STREAM_PATH,
    ErrorInfo,
    Heartbeat,
    Hello,
    LeaseEnded,
    ResetDone,
    RunCancel,
    RunStart,
    RunState,
    RunUpdate,
    ToolStatus,
    Welcome,
    WorkbenchDescriptor,
    gateway_message_adapter,
)

log = logging.getLogger(__name__)

_MAX_BACKOFF_S = 10.0
# Wait this long for cancelled runs to unwind before resetting tools anyway.
_CANCEL_GRACE_S = 2.0


@dataclass(eq=False)
class _ActiveRun:
    start: RunStart
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class Agent:
    def __init__(
        self,
        gateway_url: str,
        workbench: WorkbenchDescriptor,
        secret: str,
        tools: list[Tool],
        cameras: dict[tuple[str, str], FrameStream] | None = None,
    ) -> None:
        self._http_url = gateway_url.rstrip("/")
        self._ws_url = self._http_url.replace("https://", "wss://", 1).replace(
            "http://", "ws://", 1
        )
        self._workbench = workbench
        self._tools = {t.descriptor.name: t for t in tools}
        self._cameras = cameras or {}
        self._headers = {"Authorization": f"Bearer {secret}", "X-Workbench-Id": workbench.id}
        self._runs: dict[str, _ActiveRun] = {}
        self._send_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None
        self.connected = asyncio.Event()

    # ------------------------------------------------------------------ main

    async def run(self) -> None:
        async with httpx.AsyncClient(base_url=self._http_url, timeout=30) as http:
            self._http = http
            loops = [self._control_loop()]
            loops += [self._stream_loop(t, s, cam) for (t, s), cam in self._cameras.items()]
            try:
                await asyncio.gather(*loops)
            finally:
                await self._abort_all()

    async def _control_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with connect(
                    self._ws_url + AGENT_CONTROL_PATH,
                    additional_headers=self._headers,
                    ping_interval=10,
                    ping_timeout=10,
                ) as ws:
                    backoff = 1.0
                    await self._session(ws)
            except (OSError, ConnectionClosed, InvalidHandshake, TimeoutError) as exc:
                if isinstance(exc, InvalidStatus):
                    log.error("Gateway refused the connection: %s", exc)
                elif isinstance(exc, ConnectionClosed) and exc.rcvd is not None:
                    log.warning("Gateway closed the connection: %s", exc.rcvd)
                else:
                    log.warning("Gateway connection lost: %s", exc)
            finally:
                self.connected.clear()
                await self._abort_all()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)

    async def _session(self, ws: ClientConnection) -> None:
        await ws.send(Hello(agent_version=__version__, workbench=self._workbench).model_dump_json())
        welcome = gateway_message_adapter.validate_json(await asyncio.wait_for(ws.recv(), 10))
        if not isinstance(welcome, Welcome):
            raise ConnectionError(f"expected welcome, got {welcome.type}")
        log.info("Connected to gateway %s as %s", self._http_url, self._workbench.id)
        self.connected.set()

        heartbeat = asyncio.create_task(self._heartbeat(ws, welcome.heartbeat_interval_s))
        try:
            async for raw in ws:
                try:
                    message = gateway_message_adapter.validate_json(raw)
                except ValidationError:
                    log.warning("Ignoring invalid message from gateway: %.200r", raw)
                    continue
                await self._handle(ws, message)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _handle(self, ws: ClientConnection, message: object) -> None:
        match message:
            case RunStart(run_id=run_id) if run_id in self._runs:
                log.warning("Duplicate run.start for %s", run_id)
            case RunStart():
                active = _ActiveRun(start=message)
                self._runs[message.run_id] = active
                active.task = asyncio.create_task(self._execute(ws, active), name=message.run_id)
            case RunCancel(run_id=run_id):
                if (active := self._runs.get(run_id)) is not None:
                    active.cancel.set()
                    self._tools[active.start.tool].reset()
            case LeaseEnded(lease_id=lease_id):
                await self._cancel_runs()
                self._reset_tools()
                await self._send(ws, ResetDone(lease_id=lease_id))
            case _:
                log.warning("Unhandled gateway message %r", message)

    # ------------------------------------------------------------------ runs

    async def _execute(self, ws: ClientConnection, active: _ActiveRun) -> None:
        start = active.start
        update: RunUpdate
        try:
            tool = self._tools.get(start.tool)
            if tool is None:
                raise ActionError("unknown_tool", f"Unknown tool {start.tool!r}")
            await self._send(ws, RunUpdate(run_id=start.run_id, state=RunState.RUNNING))
            log.info("Run %s: %s.%s %s", start.run_id, start.tool, start.action, start.input)
            output = await tool.invoke(
                start.action, start.input, self._file_fetcher(start), active.cancel
            )
            update = RunUpdate(run_id=start.run_id, state=RunState.SUCCEEDED, output=output)
        except asyncio.CancelledError:
            update = RunUpdate(
                run_id=start.run_id,
                state=RunState.CANCELLED,
                error=ErrorInfo(code="cancelled", message="Cancelled"),
            )
        except ActionError as exc:
            update = RunUpdate(
                run_id=start.run_id,
                state=RunState.FAILED,
                error=ErrorInfo(code=exc.code, message=exc.message),
            )
        except Exception as exc:
            log.exception("Run %s crashed", start.run_id)
            update = RunUpdate(
                run_id=start.run_id,
                state=RunState.FAILED,
                error=ErrorInfo(code="device_error", message=str(exc)),
            )
        finally:
            self._runs.pop(start.run_id, None)
        log.info("Run %s: %s", start.run_id, update.state)
        with contextlib.suppress(ConnectionClosed):
            await self._send(ws, update)

    def _file_fetcher(self, start: RunStart) -> FileFetcher:
        async def fetch(file_id: str) -> bytes:
            url = start.files.get(file_id)
            if url is None:
                raise ActionError("invalid_input", f"No download link for file {file_id!r}")
            assert self._http is not None
            response = await self._http.get(url)
            if response.status_code != 200:
                raise ActionError("file_unavailable", f"Download failed ({response.status_code})")
            return response.content

        return fetch

    async def _cancel_runs(self) -> None:
        runs = list(self._runs.values())
        for active in runs:
            active.cancel.set()
        tasks = [a.task for a in runs if a.task is not None]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=_CANCEL_GRACE_S)
            for task in pending:
                task.cancel()

    def _reset_tools(self) -> None:
        for tool in self._tools.values():
            tool.reset()

    async def _abort_all(self) -> None:
        await self._cancel_runs()
        self._reset_tools()

    # ------------------------------------------------------------- heartbeat

    async def _heartbeat(self, ws: ClientConnection, interval: float) -> None:
        while True:
            busy = {a.start.tool for a in self._runs.values()}
            status: dict[str, ToolStatus] = {}
            for name in self._tools:
                status[name] = ToolStatus.BUSY if name in busy else ToolStatus.IDLE
            for (tool, _), camera in self._cameras.items():
                if camera.fault:
                    status[tool] = ToolStatus.FAULT
            await self._send(ws, Heartbeat(tools=status))
            await asyncio.sleep(interval)

    async def _send(self, ws: ClientConnection, message: BaseModel) -> None:
        async with self._send_lock:
            await ws.send(message.model_dump_json())

    # ---------------------------------------------------------------- video

    async def _stream_loop(self, tool: str, stream: str, camera: FrameStream) -> None:
        camera.start()
        url = self._ws_url + AGENT_STREAM_PATH.format(tool=tool, stream=stream)
        backoff = 1.0
        try:
            while True:
                try:
                    async with connect(
                        url, additional_headers=self._headers, max_size=None, ping_interval=10
                    ) as ws:
                        backoff = 1.0
                        log.info("Streaming %s/%s", tool, stream)
                        async for frame in camera.frames():
                            await ws.send(frame)
                except (OSError, ConnectionClosed, InvalidHandshake, TimeoutError) as exc:
                    log.warning("Stream %s/%s interrupted: %s", tool, stream, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
        finally:
            camera.close()
