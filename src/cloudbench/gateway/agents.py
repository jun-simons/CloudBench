"""Gateway side of the workbench agent control channel.

Each workbench agent dials in over a WebSocket, authenticates with its
per-workbench secret, sends ``hello`` with its descriptor, then exchanges
protocol messages. One live connection per workbench; a newer connection
replaces an older one (an agent that restarted before the old socket timed out).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from cloudbench.gateway.db import Workbench
from cloudbench.gateway.services import get_services
from cloudbench.gateway.util import utcnow
from cloudbench.protocol import (
    AGENT_CONTROL_PATH,
    PROTOCOL_VERSION,
    DescriptorChanged,
    Heartbeat,
    Hello,
    ResetDone,
    RunUpdate,
    ToolStatus,
    Welcome,
    WorkbenchDescriptor,
    agent_message_adapter,
)

if TYPE_CHECKING:
    from cloudbench.gateway.services import Services

log = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 10
WORKBENCH_ID_HEADER = "x-workbench-id"


@dataclass(eq=False)
class AgentConnection:
    workbench_id: str
    websocket: WebSocket
    descriptor: WorkbenchDescriptor
    agent_version: str
    tool_status: dict[str, ToolStatus] = field(default_factory=dict)
    last_message_at: float = field(default_factory=lambda: utcnow().timestamp())
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: BaseModel) -> None:
        async with self.send_lock:
            await self.websocket.send_text(message.model_dump_json())


class AgentHub:
    def __init__(self) -> None:
        self._connections: dict[str, AgentConnection] = {}
        self._services: Services | None = None

    def bind(self, services: Services) -> None:
        self._services = services

    @property
    def s(self) -> Services:
        assert self._services is not None
        return self._services

    # ----------------------------------------------------------------- queries

    def connection(self, workbench_id: str) -> AgentConnection | None:
        return self._connections.get(workbench_id)

    def online_workbenches(self) -> list[str]:
        return sorted(self._connections)

    def is_online(self, workbench_id: str) -> bool:
        return workbench_id in self._connections

    def descriptor(self, workbench_id: str) -> WorkbenchDescriptor | None:
        conn = self._connections.get(workbench_id)
        return conn.descriptor if conn else None

    async def send(self, workbench_id: str, message: BaseModel) -> bool:
        """Send a message to a workbench's agent. Returns False if it is not connected."""
        conn = self._connections.get(workbench_id)
        if conn is None:
            return False
        try:
            await conn.send(message)
        except Exception:
            log.warning("Failed to send to agent %s", workbench_id, exc_info=True)
            return False
        return True

    # ------------------------------------------------------------ auth helpers

    def check_secret(self, workbench_id: str | None, authorization: str | None) -> bool:
        if not workbench_id or not authorization:
            return False
        expected = self.s.settings.agent_secrets.get(workbench_id)
        scheme, _, token = authorization.partition(" ")
        if expected is None or scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(token.strip(), expected)

    # -------------------------------------------------------------- lifecycle

    async def serve(self, websocket: WebSocket) -> None:
        workbench_id = websocket.headers.get(WORKBENCH_ID_HEADER)
        if not self.check_secret(workbench_id, websocket.headers.get("authorization")):
            await websocket.close(code=4401, reason="invalid workbench credentials")
            return
        assert workbench_id is not None
        await websocket.accept()

        try:
            raw = await asyncio.wait_for(websocket.receive_text(), HELLO_TIMEOUT_S)
            hello = agent_message_adapter.validate_json(raw)
        except (TimeoutError, ValidationError, WebSocketDisconnect) as exc:
            log.warning("Agent %s failed handshake: %s", workbench_id, exc)
            await _close_quietly(websocket, 4400, "expected hello")
            return
        if not isinstance(hello, Hello):
            await _close_quietly(websocket, 4400, "expected hello")
            return
        if hello.workbench.id != workbench_id:
            await _close_quietly(websocket, 4403, "workbench id does not match credentials")
            return
        if hello.protocol_version != PROTOCOL_VERSION:
            await _close_quietly(websocket, 4426, f"gateway speaks protocol {PROTOCOL_VERSION}")
            return

        conn = AgentConnection(
            workbench_id=workbench_id,
            websocket=websocket,
            descriptor=hello.workbench,
            agent_version=hello.agent_version,
        )
        previous = self._connections.get(workbench_id)
        self._connections[workbench_id] = conn
        if previous is not None:
            log.info("Agent %s reconnected; closing previous connection", workbench_id)
            await _close_quietly(previous.websocket, 4409, "replaced by a newer connection")

        await self._save_descriptor(hello.workbench)
        await conn.send(Welcome(heartbeat_interval_s=self.s.settings.agent_heartbeat_interval_s))
        log.info("Agent %s online (agent %s)", workbench_id, hello.agent_version)
        self._warn_about_short_limits(hello.workbench)
        await self.s.leases.on_agent_connected(workbench_id)
        await self.publish_workbench(workbench_id)

        try:
            while True:
                raw = await websocket.receive_text()
                conn.last_message_at = utcnow().timestamp()
                try:
                    message = agent_message_adapter.validate_json(raw)
                except ValidationError:
                    log.warning("Agent %s sent an invalid message: %.200s", workbench_id, raw)
                    continue
                await self._handle(conn, message)
        except WebSocketDisconnect:
            pass
        finally:
            if self._connections.get(workbench_id) is conn:
                del self._connections[workbench_id]
                log.info("Agent %s offline", workbench_id)
                await self.s.runs.fail_inflight(
                    workbench_id, "workbench_offline", "The workbench disconnected"
                )
                await self.s.leases.on_agent_disconnected(workbench_id)
                await self.publish_workbench(workbench_id)

    async def _handle(self, conn: AgentConnection, message: object) -> None:
        match message:
            case Heartbeat(tools=tools):
                conn.tool_status = dict(tools)
            case RunUpdate():
                await self.s.runs.handle_update(conn.workbench_id, message)
            case ResetDone(lease_id=lease_id):
                await self.s.leases.on_reset_done(conn.workbench_id, lease_id)
            case DescriptorChanged(workbench=descriptor):
                if descriptor.id != conn.workbench_id:
                    log.warning("Agent %s tried to rename itself", conn.workbench_id)
                    return
                conn.descriptor = descriptor
                await self._save_descriptor(descriptor)
                await self.publish_workbench(conn.workbench_id)
            case Hello():
                log.warning("Agent %s sent a second hello; ignoring", conn.workbench_id)
            case _:
                log.warning("Unhandled agent message %r", message)

    async def check_timeouts(self) -> None:
        """Drop agents that have gone silent (called from the gateway's tick loop)."""
        cutoff = utcnow().timestamp() - self.s.settings.agent_timeout_s
        for conn in list(self._connections.values()):
            if conn.last_message_at < cutoff:
                log.warning("Agent %s timed out", conn.workbench_id)
                await _close_quietly(conn.websocket, 4408, "heartbeat timeout")

    async def close_all(self) -> None:
        for conn in list(self._connections.values()):
            await _close_quietly(conn.websocket, 1001, "gateway shutting down")

    # ---------------------------------------------------------------- helpers

    def _warn_about_short_limits(self, descriptor: WorkbenchDescriptor) -> None:
        """Warn when an agent stops actions before a lease would end.

        Users expect a live tone to run until they stop it or their lease ends; an agent
        whose ``max_duration_s`` is shorter cuts it off early with no visible reason.
        """
        lease_s = self.s.settings.lease_max_duration_s
        for tool in descriptor.tools:
            for action in tool.actions:
                cap = action.limits.get("max_duration_s")
                if isinstance(cap, int | float) and cap < lease_s:
                    log.warning(
                        "Agent %s stops %s.%s after %g s, shorter than the %g s lease; "
                        "raise vibrator.max_duration_s in the agent config",
                        descriptor.id,
                        tool.name,
                        action.name,
                        cap,
                        lease_s,
                    )

    async def _save_descriptor(self, descriptor: WorkbenchDescriptor) -> None:
        async with self.s.db.session() as s:
            row = await s.get(Workbench, descriptor.id)
            data = descriptor.model_dump(mode="json")
            if row is None:
                s.add(
                    Workbench(
                        id=descriptor.id,
                        name=descriptor.name,
                        descriptor=data,
                        maintenance=False,
                        last_seen_at=utcnow(),
                    )
                )
            else:
                row.name = descriptor.name
                row.descriptor = data
                row.last_seen_at = utcnow()
            await s.commit()

    async def publish_workbench(self, workbench_id: str) -> None:
        from cloudbench.gateway.workbenches import workbench_summary

        async with self.s.db.session() as s:
            row = await s.get(Workbench, workbench_id)
            if row is None:
                return
            summary = await workbench_summary(self.s, s, row)
        self.s.bus.publish("workbench.updated", summary.model_dump(mode="json"))


async def _close_quietly(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        log.debug("Ignoring error while closing websocket", exc_info=True)


router = APIRouter(include_in_schema=False)


@router.websocket(AGENT_CONTROL_PATH)
async def agent_control(websocket: WebSocket) -> None:
    await get_services(websocket).agents.serve(websocket)
