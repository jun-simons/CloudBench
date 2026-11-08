"""Runs: one invocation of a tool action, executed asynchronously by the agent."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloudbench.gateway.db import File, Lease, LeaseState, Principal, Run
from cloudbench.gateway.errors import ApiError, not_found
from cloudbench.gateway.files import can_read
from cloudbench.gateway.util import new_id, utcnow
from cloudbench.protocol import (
    FILE_ID_FORMAT,
    ActionDescriptor,
    ErrorInfo,
    RunCancel,
    RunStart,
    RunState,
    RunUpdate,
)

if TYPE_CHECKING:
    from cloudbench.gateway.services import Services

log = logging.getLogger(__name__)

_ALLOWED_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.PENDING: {RunState.RUNNING, RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED},
    RunState.RUNNING: {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED},
}


class RunOut(BaseModel):
    id: str
    workbench_id: str
    tool: str
    action: str
    lease_id: str
    experiment_run_id: str | None
    input: dict[str, Any]
    state: RunState
    output: dict[str, Any] | None
    error: ErrorInfo | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def run_out(r: Run) -> RunOut:
    return RunOut(
        id=r.id,
        workbench_id=r.workbench_id,
        tool=r.tool,
        action=r.action,
        lease_id=r.lease_id,
        experiment_run_id=r.experiment_run_id,
        input=r.input,
        state=RunState(r.state),
        output=r.output,
        error=ErrorInfo.model_validate(r.error) if r.error else None,
        created_at=r.created_at,
        started_at=r.started_at,
        finished_at=r.finished_at,
    )


def file_parameters(action: ActionDescriptor) -> dict[str, dict[str, Any]]:
    """Top-level input properties that take a File ID."""
    props = action.input_schema.get("properties", {})
    return {
        name: spec
        for name, spec in props.items()
        if isinstance(spec, dict) and spec.get("format") == FILE_ID_FORMAT
    }


def apply_defaults(schema: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    out = dict(value)
    for name, spec in schema.get("properties", {}).items():
        if name not in out and isinstance(spec, dict) and "default" in spec:
            out[name] = spec["default"]
    return out


class RunService:
    def __init__(self) -> None:
        self._finished: dict[str, asyncio.Event] = {}
        self._services: Services | None = None

    def bind(self, services: Services) -> None:
        self._services = services

    @property
    def s(self) -> Services:
        assert self._services is not None
        return self._services

    async def create(
        self,
        principal: Principal,
        workbench_id: str,
        tool_name: str,
        action_name: str,
        lease_id: str,
        raw_input: dict[str, Any],
        experiment_run_id: str | None = None,
    ) -> RunOut:
        descriptor = self.s.agents.descriptor(workbench_id)
        if descriptor is None:
            raise ApiError(409, "workbench_offline", "The workbench is offline")
        tool = descriptor.tool(tool_name)
        if tool is None:
            raise not_found("tool", tool_name)
        action = tool.action(action_name)
        if action is None:
            raise not_found("action", action_name)

        validator = Draft202012Validator(action.input_schema)
        errors = sorted(validator.iter_errors(raw_input), key=lambda e: list(e.path))
        if errors:
            raise ApiError(
                422,
                "invalid_input",
                "Input does not match the action's input_schema",
                [{"path": list(e.path), "message": e.message} for e in errors],
            )
        run_input = apply_defaults(action.input_schema, raw_input)

        async with self.s.db.session() as s:
            lease = await s.get(Lease, lease_id)
            if lease is None or lease.principal_id != principal.id:
                raise ApiError(403, "lease_required", "You need an active lease on this workbench")
            if lease.workbench_id != workbench_id:
                raise ApiError(403, "lease_required", "That lease is for a different workbench")
            if lease.state != LeaseState.ACTIVE:
                raise ApiError(409, "lease_not_active", f"Lease is {lease.state}")

            files = await self._resolve_files(s, principal, action, run_input)

            busy = await s.execute(
                select(Run.id, Run.action).where(
                    Run.workbench_id == workbench_id,
                    Run.tool == tool_name,
                    Run.state.in_([RunState.PENDING, RunState.RUNNING]),
                )
            )
            # Concurrent actions never block and are never blocked by long-running ones.
            blocking = [
                row for row in busy if not (a := tool.action(row.action)) or not a.concurrent
            ]
            if not action.concurrent and blocking:
                raise ApiError(409, "tool_busy", f"Tool {tool_name!r} is already running an action")

            run = Run(
                id=new_id("run"),
                workbench_id=workbench_id,
                tool=tool_name,
                action=action_name,
                lease_id=lease_id,
                principal_id=principal.id,
                experiment_run_id=experiment_run_id,
                input=run_input,
                state=RunState.PENDING,
                created_at=utcnow(),
            )
            s.add(run)
            await s.commit()

        self._finished[run.id] = asyncio.Event()
        sent = await self.s.agents.send(
            workbench_id,
            RunStart(
                run_id=run.id,
                lease_id=lease_id,
                tool=tool_name,
                action=action_name,
                input=run_input,
                files=files,
            ),
        )
        if not sent:
            await self._apply(
                workbench_id,
                RunUpdate(
                    run_id=run.id,
                    state=RunState.FAILED,
                    error=ErrorInfo(
                        code="workbench_offline", message="Could not reach the workbench"
                    ),
                ),
            )
        else:
            self._publish(run)
        async with self.s.db.session() as s:
            fresh = await s.get(Run, run.id)
            assert fresh is not None
            return run_out(fresh)

    async def _resolve_files(
        self,
        s: AsyncSession,
        principal: Principal,
        action: ActionDescriptor,
        run_input: dict[str, Any],
    ) -> dict[str, str]:
        files: dict[str, str] = {}
        for name, spec in file_parameters(action).items():
            file_id = run_input.get(name)
            if file_id is None:
                continue
            f = await s.get(File, file_id)
            if f is None or not can_read(principal, f):
                raise ApiError(422, "invalid_input", f"{name}: file {file_id!r} not found")
            expected = spec.get("contentMediaType")
            if expected and f.media_type != expected:
                raise ApiError(
                    422, "invalid_input", f"{name}: expected {expected}, got {f.media_type}"
                )
            files[f.id] = self.s.files.signed_path(f.id)
        return files

    async def get(self, principal: Principal, run_id: str) -> RunOut:
        async with self.s.db.session() as s:
            run = await s.get(Run, run_id)
        if run is None or run.principal_id != principal.id:
            raise not_found("run", run_id)
        return run_out(run)

    async def list_for(self, principal: Principal, limit: int = 50) -> list[RunOut]:
        async with self.s.db.session() as s:
            result = await s.execute(
                select(Run)
                .where(Run.principal_id == principal.id)
                .order_by(Run.created_at.desc())
                .limit(limit)
            )
            return [run_out(r) for r in result.scalars()]

    async def cancel(self, principal: Principal, run_id: str) -> RunOut:
        run = await self.get(principal, run_id)
        if not run.state.terminal:
            await self.request_cancel(run.workbench_id, run.id)
        return await self.get(principal, run_id)

    async def request_cancel(self, workbench_id: str, run_id: str) -> None:
        if not await self.s.agents.send(workbench_id, RunCancel(run_id=run_id)):
            await self._apply(
                workbench_id,
                RunUpdate(
                    run_id=run_id,
                    state=RunState.CANCELLED,
                    error=ErrorInfo(code="cancelled", message="Cancelled"),
                ),
            )

    async def wait(self, run_id: str) -> RunOut:
        """Wait until a run reaches a terminal state."""
        event = self._finished.get(run_id)
        if event is not None:
            await event.wait()
        async with self.s.db.session() as s:
            run = await s.get(Run, run_id)
            if run is None:
                raise not_found("run", run_id)
            return run_out(run)

    # ------------------------------------------------------------ agent input

    async def handle_update(self, workbench_id: str, update: RunUpdate) -> None:
        await self._apply(workbench_id, update)

    async def fail_inflight(self, workbench_id: str | None, code: str, message: str) -> None:
        """Fail every unfinished run on a workbench (or everywhere, if None)."""
        async with self.s.db.session() as s:
            query = select(Run).where(Run.state.in_([RunState.PENDING, RunState.RUNNING]))
            if workbench_id is not None:
                query = query.where(Run.workbench_id == workbench_id)
            runs = list((await s.execute(query)).scalars())
        for run in runs:
            await self._apply(
                run.workbench_id,
                RunUpdate(
                    run_id=run.id,
                    state=RunState.FAILED,
                    error=ErrorInfo(code=code, message=message),
                ),
            )

    async def _apply(self, workbench_id: str, update: RunUpdate) -> None:
        async with self.s.db.session() as s:
            run = await s.get(Run, update.run_id)
            if run is None or run.workbench_id != workbench_id:
                log.warning(
                    "Ignoring update for unknown run %s from %s", update.run_id, workbench_id
                )
                return
            current = RunState(run.state)
            if update.state == current:
                return
            if update.state not in _ALLOWED_TRANSITIONS.get(current, set()):
                log.warning("Ignoring run %s transition %s -> %s", run.id, current, update.state)
                return
            now = utcnow()
            run.state = update.state
            if update.state == RunState.RUNNING:
                run.started_at = now
            if update.state.terminal:
                run.finished_at = now
                run.output = update.output
                run.error = update.error.model_dump() if update.error else None
            await s.commit()
        self._publish(run)
        if update.state.terminal:
            event = self._finished.pop(run.id, None)
            if event is not None:
                event.set()

    def _publish(self, run: Run) -> None:
        self.s.bus.publish("run.updated", run_out(run).model_dump(mode="json"), to=run.principal_id)
        if run.experiment_run_id is not None:
            self.s.experiments.on_step_updated(run.experiment_run_id, run.principal_id)
