from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloudbench.gateway.db import LeaseState, Principal, Workbench, WorkbenchStatus
from cloudbench.gateway.errors import not_found
from cloudbench.gateway.services import Services, get_services
from cloudbench.protocol import ToolDescriptor, ToolStatus, WorkbenchDescriptor


class ToolOut(ToolDescriptor):
    status: ToolStatus | None = None


class WorkbenchOut(BaseModel):
    id: str
    name: str
    description: str
    status: WorkbenchStatus
    # True while the bench is being reset to a safe state between leases.
    resetting: bool
    queue_length: int
    # True when a new lease request would be granted immediately.
    available: bool
    # Display name of whoever is using the bench right now (active lease only).
    current_holder: str | None
    last_seen_at: datetime | None
    tools: list[ToolOut]


def workbench_status(services: Services, row: Workbench) -> WorkbenchStatus:
    if row.maintenance:
        return WorkbenchStatus.MAINTENANCE
    if services.agents.is_online(row.id):
        return WorkbenchStatus.ONLINE
    return WorkbenchStatus.OFFLINE


async def workbench_summary(services: Services, s: AsyncSession, row: Workbench) -> WorkbenchOut:
    descriptor = WorkbenchDescriptor.model_validate(row.descriptor)
    conn = services.agents.connection(row.id)
    tool_status = conn.tool_status if conn else {}
    active = await services.leases.active_lease(s, row.id)
    queue_length = await services.leases.queue_length(s, row.id)
    status = workbench_status(services, row)
    holder = None
    if active is not None and active.state == LeaseState.ACTIVE:
        holder = await s.get(Principal, active.principal_id)
    return WorkbenchOut(
        id=row.id,
        name=row.name,
        description=descriptor.description,
        status=status,
        resetting=services.leases.is_resetting(row.id),
        queue_length=queue_length,
        available=(
            status == WorkbenchStatus.ONLINE
            and active is None
            and queue_length == 0
            and not services.leases.is_resetting(row.id)
        ),
        current_holder=holder.display_name if holder else None,
        last_seen_at=row.last_seen_at,
        tools=[ToolOut(**t.model_dump(), status=tool_status.get(t.name)) for t in descriptor.tools],
    )


router = APIRouter(tags=["workbenches"])


@router.get("/v1/workbenches")
async def list_workbenches(
    services: Annotated[Services, Depends(get_services)],
) -> list[WorkbenchOut]:
    async with services.db.session() as s:
        rows = (await s.execute(select(Workbench).order_by(Workbench.id))).scalars().all()
        return [await workbench_summary(services, s, row) for row in rows]


@router.get("/v1/workbenches/{workbench_id}")
async def get_workbench(
    workbench_id: str, services: Annotated[Services, Depends(get_services)]
) -> WorkbenchOut:
    async with services.db.session() as s:
        row = await s.get(Workbench, workbench_id)
        if row is None:
            raise not_found("workbench", workbench_id)
        return await workbench_summary(services, s, row)


@router.get("/v1/workbenches/{workbench_id}/tools/{tool}")
async def get_tool(
    workbench_id: str, tool: str, services: Annotated[Services, Depends(get_services)]
) -> ToolOut:
    bench = await get_workbench(workbench_id, services)
    found = next((t for t in bench.tools if t.name == tool), None)
    if found is None:
        raise not_found("tool", tool)
    return found
