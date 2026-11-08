"""Live video: agents push JPEG frames in, viewers get MJPEG out.

The agent sends each frame once over a dedicated WebSocket; the gateway keeps
only the latest frame per stream and every viewer is served from it. A slow
viewer simply skips frames instead of buffering, so one bad connection never
delays anyone else, and latency stays bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from cloudbench.gateway.agents import WORKBENCH_ID_HEADER
from cloudbench.gateway.auth import OptionalPrincipal
from cloudbench.gateway.db import Lease, LeaseState, Principal
from cloudbench.gateway.errors import ApiError, not_found
from cloudbench.gateway.services import Services, get_services
from cloudbench.protocol import AGENT_STREAM_PATH, StreamAccess, StreamDescriptor

log = logging.getLogger(__name__)

BOUNDARY = "cloudbenchframe"
# Viewers with no new frame for this long get the last frame re-sent, which keeps
# proxies from closing an idle connection and lets the client notice a dead link.
_REPEAT_AFTER_S = 5.0


@dataclass(eq=False)
class Channel:
    frame: bytes | None = None
    seq: int = 0
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    producers: int = 0

    async def publish(self, frame: bytes) -> None:
        async with self.condition:
            self.frame = frame
            self.seq += 1
            self.condition.notify_all()

    async def next_frame(self, after: int) -> tuple[int, bytes | None]:
        async with self.condition:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self.condition.wait_for(lambda: self.seq > after), _REPEAT_AFTER_S
                )
            return self.seq, self.frame


class StreamHub:
    def __init__(self) -> None:
        self._channels: dict[tuple[str, str, str], Channel] = {}

    def channel(self, workbench_id: str, tool: str, stream: str) -> Channel:
        key = (workbench_id, tool, stream)
        channel = self._channels.get(key)
        if channel is None:
            channel = self._channels[key] = Channel()
        return channel

    async def mjpeg(self, channel: Channel) -> AsyncIterator[bytes]:
        seq = 0
        while True:
            seq, frame = await channel.next_frame(seq)
            if frame is None:
                continue
            yield (
                (
                    f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode()
                + frame
                + b"\r\n"
            )


router = APIRouter()


async def _check_access(
    services: Services,
    principal: Principal | None,
    workbench_id: str,
    stream: StreamDescriptor,
) -> None:
    if stream.access == StreamAccess.PUBLIC:
        return
    if principal is None:
        raise ApiError(401, "authentication_required", "This stream requires a lease")
    async with services.db.session() as s:
        result = await s.execute(
            select(Lease.id).where(
                Lease.workbench_id == workbench_id,
                Lease.principal_id == principal.id,
                Lease.state == LeaseState.ACTIVE,
            )
        )
        if result.first() is None:
            raise ApiError(403, "lease_required", "This stream requires an active lease")


def _stream_descriptor(
    services: Services, workbench_id: str, tool_name: str, stream_name: str
) -> StreamDescriptor:
    descriptor = services.agents.descriptor(workbench_id)
    if descriptor is None:
        raise ApiError(409, "workbench_offline", "The workbench is offline")
    tool = descriptor.tool(tool_name)
    if tool is None:
        raise not_found("tool", tool_name)
    stream = tool.stream(stream_name)
    if stream is None:
        raise not_found("stream", stream_name)
    return stream


@router.get("/v1/workbenches/{workbench_id}/tools/{tool}/streams/{stream}", tags=["streams"])
async def watch_stream(
    workbench_id: str,
    tool: str,
    stream: str,
    request: Request,
    principal: OptionalPrincipal,
    services: Annotated[Services, Depends(get_services)],
) -> StreamingResponse:
    """Live MJPEG stream. Render it with ``<img src=...>``."""
    descriptor = _stream_descriptor(services, workbench_id, tool, stream)
    await _check_access(services, principal, workbench_id, descriptor)
    channel = services.streams.channel(workbench_id, tool, stream)

    async def body() -> AsyncIterator[bytes]:
        async for chunk in services.streams.mjpeg(channel):
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(
        body(),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/v1/workbenches/{workbench_id}/tools/{tool}/streams/{stream}/frame.jpg", tags=["streams"]
)
async def latest_frame(
    workbench_id: str,
    tool: str,
    stream: str,
    principal: OptionalPrincipal,
    services: Annotated[Services, Depends(get_services)],
) -> Response:
    """The most recent frame as a single JPEG (useful for AI agents and thumbnails)."""
    descriptor = _stream_descriptor(services, workbench_id, tool, stream)
    await _check_access(services, principal, workbench_id, descriptor)
    frame = services.streams.channel(workbench_id, tool, stream).frame
    if frame is None:
        raise ApiError(503, "no_frame", "No frame received from the camera yet")
    return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.websocket(AGENT_STREAM_PATH)
async def agent_stream(websocket: WebSocket, tool: str, stream: str) -> None:
    services = get_services(websocket)
    workbench_id = websocket.headers.get(WORKBENCH_ID_HEADER)
    if not services.agents.check_secret(workbench_id, websocket.headers.get("authorization")):
        await websocket.close(code=4401, reason="invalid workbench credentials")
        return
    assert workbench_id is not None
    await websocket.accept()
    channel = services.streams.channel(workbench_id, tool, stream)
    channel.producers += 1
    log.info("Stream %s/%s/%s connected", workbench_id, tool, stream)
    try:
        while True:
            frame = await websocket.receive_bytes()
            await channel.publish(frame)
    except WebSocketDisconnect:
        pass
    finally:
        channel.producers -= 1
        log.info("Stream %s/%s/%s disconnected", workbench_id, tool, stream)
