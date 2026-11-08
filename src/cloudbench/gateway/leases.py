"""Leases: exclusive, time-limited access to a workbench, with a FIFO queue.

A lease is both the queue ticket and the access grant::

    queued --(head of queue, bench free)--> offered --(accept)--> active
       |                                       |                    |
       +---------------------------------------+--------------------+--> ended

    ended = released | expired | revoked

* A request when the bench is free and nobody is waiting is granted ``active`` at once.
* Otherwise it is ``queued``. When the bench frees up, the oldest queued lease is
  ``offered`` and has ``lease_offer_timeout_s`` to accept, so a judge whose phone
  went to sleep does not hold everyone up.
* When an ``active`` lease ends for any reason, the agent is told to reset every
  tool to its safe state, and the next lease is offered only after it confirms
  (or after ``reset_timeout_s``).

All transitions run under one lock; traffic is a handful of requests per second
at most, so simplicity wins over finer-grained locking.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cloudbench.gateway.db import OPEN_LEASE_STATES, Lease, LeaseState, Principal, Workbench
from cloudbench.gateway.errors import ApiError, not_found
from cloudbench.gateway.util import new_id, utcnow
from cloudbench.protocol import LeaseEnded

if TYPE_CHECKING:
    from cloudbench.gateway.services import Services

log = logging.getLogger(__name__)


class LeaseOut(BaseModel):
    id: str
    workbench_id: str
    holder: str
    state: LeaseState
    queue_position: int | None
    queue_length: int
    created_at: datetime
    offer_expires_at: datetime | None
    granted_at: datetime | None
    expires_at: datetime | None
    ended_at: datetime | None
    end_reason: str | None


@dataclass
class _Reset:
    lease_id: str
    deadline: datetime


class LeaseManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._resetting: dict[str, _Reset] = {}
        # Leases that ended since their holder was last notified.
        self._ended_unnotified: set[str] = set()
        self._services: Services | None = None

    def bind(self, services: Services) -> None:
        self._services = services

    @property
    def s(self) -> Services:
        assert self._services is not None
        return self._services

    # ------------------------------------------------------------------ reads

    async def get(self, session: AsyncSession, lease_id: str) -> Lease:
        lease = await session.get(Lease, lease_id)
        if lease is None:
            raise not_found("lease", lease_id)
        return lease

    async def to_out(self, session: AsyncSession, lease: Lease) -> LeaseOut:
        queued = await self._queued(session, lease.workbench_id)
        position = None
        if lease.state == LeaseState.QUEUED:
            position = next(i for i, q in enumerate(queued, start=1) if q.id == lease.id)
        holder = await session.get(Principal, lease.principal_id)
        return LeaseOut(
            id=lease.id,
            workbench_id=lease.workbench_id,
            holder=holder.display_name if holder else "",
            state=LeaseState(lease.state),
            queue_position=position,
            queue_length=len(queued),
            created_at=lease.created_at,
            offer_expires_at=lease.offer_expires_at,
            granted_at=lease.granted_at,
            expires_at=lease.expires_at,
            ended_at=lease.ended_at,
            end_reason=lease.end_reason,
        )

    async def active_lease(self, session: AsyncSession, workbench_id: str) -> Lease | None:
        result = await session.execute(
            select(Lease).where(
                Lease.workbench_id == workbench_id,
                Lease.state.in_([LeaseState.ACTIVE, LeaseState.OFFERED]),
            )
        )
        return result.scalars().first()

    async def queue_length(self, session: AsyncSession, workbench_id: str) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(Lease)
            .where(Lease.workbench_id == workbench_id, Lease.state == LeaseState.QUEUED)
        )
        return int(result.scalar_one())

    def is_resetting(self, workbench_id: str) -> bool:
        return workbench_id in self._resetting

    # ---------------------------------------------------------------- commands

    async def request(self, principal: Principal, workbench_id: str) -> tuple[LeaseOut, bool]:
        """Join the queue (or get the bench at once). Returns (lease, created)."""
        async with self._lock, self.s.db.session() as s:
            workbench = await s.get(Workbench, workbench_id)
            if workbench is None:
                raise not_found("workbench", workbench_id)
            if workbench.maintenance:
                raise ApiError(409, "workbench_unavailable", "The workbench is in maintenance")

            existing = await s.execute(
                select(Lease).where(
                    Lease.workbench_id == workbench_id,
                    Lease.principal_id == principal.id,
                    Lease.state.in_(OPEN_LEASE_STATES),
                )
            )
            lease = existing.scalars().first()
            if lease is not None:
                return await self.to_out(s, lease), False

            now = utcnow()
            lease = Lease(
                id=new_id("lease"),
                workbench_id=workbench_id,
                principal_id=principal.id,
                state=LeaseState.QUEUED,
                created_at=now,
            )
            if await self._available(s, workbench) and not await self._queued(s, workbench_id):
                self._activate(lease, now)
            s.add(lease)
            await s.commit()
            out = await self.to_out(s, lease)
        await self._publish(workbench_id)
        return out, True

    async def accept(self, principal: Principal, lease_id: str) -> LeaseOut:
        async with self._lock, self.s.db.session() as s:
            lease = await self._own(s, principal, lease_id)
            if lease.state == LeaseState.OFFERED:
                self._activate(lease, utcnow())
                await s.commit()
            elif lease.state != LeaseState.ACTIVE:
                raise ApiError(
                    409, "lease_not_offered", f"Lease is {lease.state}; only offers can be accepted"
                )
            out = await self.to_out(s, lease)
        await self._publish(lease.workbench_id)
        return out

    async def renew(self, principal: Principal, lease_id: str) -> LeaseOut:
        async with self._lock, self.s.db.session() as s:
            lease = await self._own(s, principal, lease_id)
            if lease.state != LeaseState.ACTIVE:
                raise ApiError(409, "lease_not_active", f"Lease is {lease.state}")
            if await self._queued(s, lease.workbench_id):
                raise ApiError(
                    409, "queue_not_empty", "Others are waiting; the lease cannot be renewed"
                )
            lease.expires_at = utcnow() + timedelta(seconds=self.s.settings.lease_max_duration_s)
            await s.commit()
            out = await self.to_out(s, lease)
        await self._publish(lease.workbench_id)
        return out

    async def release(self, principal: Principal, lease_id: str) -> LeaseOut:
        async with self._lock, self.s.db.session() as s:
            lease = await self._own(s, principal, lease_id)
            if not LeaseState(lease.state).terminal:
                await self._end(s, lease, LeaseState.RELEASED, "released")
                await s.commit()
            out = await self.to_out(s, lease)
        await self._after_change(lease.workbench_id)
        return out

    async def revoke(self, lease_id: str, reason: str = "revoked_by_admin") -> LeaseOut:
        async with self._lock, self.s.db.session() as s:
            lease = await self.get(s, lease_id)
            if not LeaseState(lease.state).terminal:
                await self._end(s, lease, LeaseState.REVOKED, reason)
                await s.commit()
            out = await self.to_out(s, lease)
        await self._after_change(lease.workbench_id)
        return out

    async def set_maintenance(self, workbench_id: str, maintenance: bool) -> None:
        async with self._lock, self.s.db.session() as s:
            workbench = await s.get(Workbench, workbench_id)
            if workbench is None:
                raise not_found("workbench", workbench_id)
            workbench.maintenance = maintenance
            if maintenance:
                open_leases = await s.execute(
                    select(Lease).where(
                        Lease.workbench_id == workbench_id, Lease.state.in_(OPEN_LEASE_STATES)
                    )
                )
                for lease in open_leases.scalars():
                    await self._end(s, lease, LeaseState.REVOKED, "workbench_maintenance")
            await s.commit()
        await self._after_change(workbench_id)
        await self.s.agents.publish_workbench(workbench_id)

    # ----------------------------------------------------------- agent events

    async def on_agent_connected(self, workbench_id: str) -> None:
        # A freshly connected agent starts with every tool in its safe state.
        self._resetting.pop(workbench_id, None)
        await self._after_change(workbench_id)

    async def on_agent_disconnected(self, workbench_id: str) -> None:
        async with self._lock, self.s.db.session() as s:
            self._resetting.pop(workbench_id, None)
            lease = await self.active_lease(s, workbench_id)
            if lease is not None:
                await self._end(s, lease, LeaseState.REVOKED, "workbench_offline")
                await s.commit()
        await self._publish(workbench_id)

    async def on_reset_done(self, workbench_id: str, lease_id: str) -> None:
        pending = self._resetting.get(workbench_id)
        if pending is None or pending.lease_id != lease_id:
            return
        del self._resetting[workbench_id]
        await self._after_change(workbench_id)

    # ------------------------------------------------------------------- tick

    async def tick(self) -> None:
        """Enforce offer timeouts, lease durations, idle holders and reset deadlines."""
        now = utcnow()
        changed: set[str] = set()

        for workbench_id, pending in list(self._resetting.items()):
            if pending.deadline <= now:
                log.warning("Workbench %s did not confirm its reset in time", workbench_id)
                del self._resetting[workbench_id]
                changed.add(workbench_id)

        async with self._lock, self.s.db.session() as s:
            result = await s.execute(
                select(Lease).where(Lease.state.in_([LeaseState.OFFERED, LeaseState.ACTIVE]))
            )
            idle_limit = self.s.settings.lease_idle_timeout_s
            for lease in result.scalars():
                if lease.state == LeaseState.OFFERED:
                    if lease.offer_expires_at is not None and lease.offer_expires_at <= now:
                        await self._end(s, lease, LeaseState.EXPIRED, "offer_timeout")
                        changed.add(lease.workbench_id)
                elif lease.expires_at is not None and lease.expires_at <= now:
                    await self._end(s, lease, LeaseState.EXPIRED, "duration_exceeded")
                    changed.add(lease.workbench_id)
                elif self.s.presence.idle_for(lease.principal_id) > idle_limit:
                    await self._end(s, lease, LeaseState.RELEASED, "holder_idle")
                    changed.add(lease.workbench_id)
            await s.commit()

        for workbench_id in changed:
            await self._after_change(workbench_id)

    async def recover_after_restart(self) -> None:
        """Leases that were offered or active when the gateway stopped cannot be trusted."""
        async with self.s.db.session() as s:
            result = await s.execute(
                select(Lease).where(Lease.state.in_([LeaseState.OFFERED, LeaseState.ACTIVE]))
            )
            for lease in result.scalars():
                lease.state = LeaseState.REVOKED
                lease.ended_at = utcnow()
                lease.end_reason = "gateway_restarted"
            await s.commit()

    # -------------------------------------------------------------- internals

    def _activate(self, lease: Lease, now: datetime) -> None:
        lease.state = LeaseState.ACTIVE
        lease.granted_at = now
        lease.offer_expires_at = None
        lease.expires_at = now + timedelta(seconds=self.s.settings.lease_max_duration_s)

    async def _own(self, s: AsyncSession, principal: Principal, lease_id: str) -> Lease:
        lease = await self.get(s, lease_id)
        if lease.principal_id != principal.id:
            # Do not reveal other people's lease IDs.
            raise not_found("lease", lease_id)
        return lease

    async def _queued(self, s: AsyncSession, workbench_id: str) -> list[Lease]:
        result = await s.execute(
            select(Lease)
            .where(Lease.workbench_id == workbench_id, Lease.state == LeaseState.QUEUED)
            .order_by(Lease.created_at, Lease.id)
        )
        return list(result.scalars())

    async def _available(self, s: AsyncSession, workbench: Workbench) -> bool:
        return (
            not workbench.maintenance
            and self.s.agents.is_online(workbench.id)
            and workbench.id not in self._resetting
            and await self.active_lease(s, workbench.id) is None
        )

    async def _end(self, s: AsyncSession, lease: Lease, state: LeaseState, reason: str) -> None:
        was_active = lease.state == LeaseState.ACTIVE
        lease.state = state
        lease.ended_at = utcnow()
        lease.end_reason = reason
        self._ended_unnotified.add(lease.id)
        log.info("Lease %s on %s ended: %s (%s)", lease.id, lease.workbench_id, state, reason)
        if was_active and self.s.agents.is_online(lease.workbench_id):
            self._resetting[lease.workbench_id] = _Reset(
                lease_id=lease.id,
                deadline=utcnow() + timedelta(seconds=self.s.settings.reset_timeout_s),
            )
            await self.s.agents.send(lease.workbench_id, LeaseEnded(lease_id=lease.id))
        if was_active:
            await self.s.experiments.on_lease_ended(lease.id)

    async def _promote(self, workbench_id: str) -> None:
        async with self._lock, self.s.db.session() as s:
            workbench = await s.get(Workbench, workbench_id)
            if workbench is None or not await self._available(s, workbench):
                return
            queued = await self._queued(s, workbench_id)
            if not queued:
                return
            now = utcnow()
            head = queued[0]
            head.state = LeaseState.OFFERED
            head.offered_at = now
            head.offer_expires_at = now + timedelta(seconds=self.s.settings.lease_offer_timeout_s)
            await s.commit()

    async def _after_change(self, workbench_id: str) -> None:
        await self._promote(workbench_id)
        await self._publish(workbench_id)

    async def _publish(self, workbench_id: str) -> None:
        """Push fresh lease state to everyone with an open lease on the bench, plus a summary."""
        async with self.s.db.session() as s:
            result = await s.execute(
                select(Lease).where(
                    Lease.workbench_id == workbench_id,
                    Lease.state.in_(OPEN_LEASE_STATES) | Lease.id.in_(self._ended_unnotified),
                )
            )
            for lease in result.scalars():
                self._ended_unnotified.discard(lease.id)
                out = await self.to_out(s, lease)
                self.s.bus.publish(
                    "lease.updated", out.model_dump(mode="json"), to=lease.principal_id
                )
        await self.s.agents.publish_workbench(workbench_id)
