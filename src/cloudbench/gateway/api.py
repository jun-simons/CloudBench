"""Public routes for sessions, events, leases, runs and admin."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from cloudbench.gateway.auth import (
    CurrentPrincipal,
    OptionalPrincipal,
    PrincipalOut,
    SessionCreate,
    SessionOut,
    create_session,
    principal_out,
    require_admin,
)
from cloudbench.gateway.errors import not_found
from cloudbench.gateway.leases import LeaseOut
from cloudbench.gateway.runs import RunOut
from cloudbench.gateway.services import Services, get_services

ServicesDep = Annotated[Services, Depends(get_services)]

router = APIRouter()


# ---------------------------------------------------------------- sessions


@router.post("/v1/sessions", status_code=201, tags=["sessions"])
async def new_session(body: SessionCreate, services: ServicesDep) -> SessionOut:
    """Start a guest session. Send the returned token as ``Authorization: Bearer``."""
    return await create_session(services, body.display_name)


@router.get("/v1/me", tags=["sessions"])
async def me(principal: CurrentPrincipal) -> PrincipalOut:
    return principal_out(principal)


# ------------------------------------------------------------------ events


@router.get("/v1/events", tags=["events"])
async def events(
    request: Request, principal: OptionalPrincipal, services: ServicesDep
) -> StreamingResponse:
    """Server-Sent Events: ``lease.updated``, ``run.updated``, ``experiment_run.updated``
    (for the caller) and ``workbench.updated`` (for everyone).

    An open stream also counts as presence for lease idle timeouts.
    """
    sub = services.bus.subscribe(principal.id if principal else None)
    keepalive = services.settings.sse_keepalive_s

    async def body() -> AsyncIterator[str]:
        try:
            yield "retry: 2000\n: connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(sub.queue.get(), keepalive)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keepalive\n\n"
                    continue
                if event is None:  # subscriber overflowed; client will reconnect
                    return
                yield event.encode()
        finally:
            services.bus.unsubscribe(sub)
            if principal is not None:
                services.presence.touch(principal.id)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ leases


@router.post("/v1/workbenches/{workbench_id}/leases", tags=["leases"])
async def request_lease(
    workbench_id: str, principal: CurrentPrincipal, services: ServicesDep, response: Response
) -> LeaseOut:
    """Join the workbench queue. Returns ``201`` with a new lease (``active`` if the bench
    was free, else ``queued``), or ``200`` with your existing open lease."""
    lease, created = await services.leases.request(principal, workbench_id)
    response.status_code = 201 if created else 200
    return lease


@router.get("/v1/me/leases", tags=["library"])
async def list_my_leases(
    principal: CurrentPrincipal, services: ServicesDep, open: bool = True
) -> list[LeaseOut]:
    """Your leases, newest first. By default only open ones (queued, offered, active)."""
    return await services.leases.list_for(principal, open_only=open)


@router.get("/v1/leases/{lease_id}", tags=["leases"])
async def get_lease(lease_id: str, principal: CurrentPrincipal, services: ServicesDep) -> LeaseOut:
    async with services.db.session() as s:
        lease = await services.leases.get(s, lease_id)
        if lease.principal_id != principal.id:
            raise not_found("lease", lease_id)
        return await services.leases.to_out(s, lease)


@router.post("/v1/leases/{lease_id}/accept", tags=["leases"])
async def accept_lease(
    lease_id: str, principal: CurrentPrincipal, services: ServicesDep
) -> LeaseOut:
    return await services.leases.accept(principal, lease_id)


@router.post("/v1/leases/{lease_id}/renew", tags=["leases"])
async def renew_lease(
    lease_id: str, principal: CurrentPrincipal, services: ServicesDep
) -> LeaseOut:
    return await services.leases.renew(principal, lease_id)


@router.delete("/v1/leases/{lease_id}", tags=["leases"])
async def release_lease(
    lease_id: str, principal: CurrentPrincipal, services: ServicesDep
) -> LeaseOut:
    """Leave the queue, decline an offer, or release the workbench."""
    return await services.leases.release(principal, lease_id)


# -------------------------------------------------------------------- runs


class RunCreate(BaseModel):
    lease_id: str
    input: dict[str, Any] = Field(default_factory=dict)


@router.post(
    "/v1/workbenches/{workbench_id}/tools/{tool}/actions/{action}/runs",
    status_code=202,
    tags=["runs"],
)
async def create_run(
    workbench_id: str,
    tool: str,
    action: str,
    body: RunCreate,
    principal: CurrentPrincipal,
    services: ServicesDep,
) -> RunOut:
    """Invoke an action. Returns at once with the run in ``pending``; follow it via
    ``run.updated`` events or ``GET /v1/runs/{id}``."""
    return await services.runs.create(
        principal, workbench_id, tool, action, body.lease_id, body.input
    )


@router.get("/v1/runs/{run_id}", tags=["runs"])
async def get_run(run_id: str, principal: CurrentPrincipal, services: ServicesDep) -> RunOut:
    return await services.runs.get(principal, run_id)


@router.post("/v1/runs/{run_id}/cancel", tags=["runs"])
async def cancel_run(run_id: str, principal: CurrentPrincipal, services: ServicesDep) -> RunOut:
    return await services.runs.cancel(principal, run_id)


@router.get("/v1/me/runs", tags=["library"])
async def list_my_runs(principal: CurrentPrincipal, services: ServicesDep) -> list[RunOut]:
    return await services.runs.list_for(principal)


# ------------------------------------------------------------------- admin


class MaintenanceBody(BaseModel):
    maintenance: bool


admin = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@admin.post("/workbenches/{workbench_id}/maintenance", status_code=204)
async def set_maintenance(workbench_id: str, body: MaintenanceBody, services: ServicesDep) -> None:
    await services.leases.set_maintenance(workbench_id, body.maintenance)


@admin.delete("/leases/{lease_id}")
async def revoke_lease(lease_id: str, services: ServicesDep) -> LeaseOut:
    return await services.leases.revoke(lease_id)


router.include_router(admin)
