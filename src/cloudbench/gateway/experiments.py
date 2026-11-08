"""Experiments: portable, declarative recipes that run on any compatible workbench.

An experiment file names the tool *types* it needs (as roles), the parameters a
student may set (JSON Schema), its assets (static files or waveforms generated
from parameters), and a flat procedure of steps. Templating is deliberately
tiny: a string that is exactly ``{{ params.x }}``, ``{{ assets.x }}`` or
``{{ assets[params.x] }}`` is replaced by that value; nothing else is evaluated.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select

from cloudbench.gateway.auth import CurrentPrincipal
from cloudbench.gateway.db import ExperimentRun, File, Lease, LeaseState, Principal, Run
from cloudbench.gateway.errors import ApiError, not_found
from cloudbench.gateway.runs import RunOut, run_out
from cloudbench.gateway.services import Services, get_services
from cloudbench.gateway.util import new_id, utcnow
from cloudbench.gateway.waveforms import SineSpec, sine_wav
from cloudbench.protocol import ErrorInfo, RunState, WorkbenchDescriptor

log = logging.getLogger(__name__)

_EXPR = re.compile(
    r"^\{\{\s*(?:params\.(?P<param>\w+)|assets\.(?P<asset>\w+)|assets\[params\.(?P<indirect>\w+)\])\s*\}\}$"
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Definition format
# --------------------------------------------------------------------------- #


class ToolRequirement(_Strict):
    type: str
    actions: list[str] = Field(default_factory=list)
    streams: list[str] = Field(default_factory=list)


class Requirements(_Strict):
    tools: dict[str, ToolRequirement]


class FileAsset(_Strict):
    file: str


class GeneratedAsset(_Strict):
    # Fields may be literal values or "{{ params.x }}" expressions.
    generate: dict[str, Any]


Asset = FileAsset | GeneratedAsset


class ShowStep(_Strict):
    show: str = Field(pattern=r"^\w+\.\w+$", description="role.stream to display")


class RunStep(_Strict):
    run: str = Field(pattern=r"^\w+\.\w+$", description="role.action to invoke")
    input: dict[str, Any] = Field(default_factory=dict)


Step = ShowStep | RunStep


class ExperimentDefinition(_Strict):
    apiVersion: Literal["cloudbench/v1"]
    kind: Literal["Experiment"]
    id: str = Field(pattern=r"^exp_[a-z0-9_]{1,60}$")
    version: int = Field(ge=1)
    title: str
    summary: str = ""
    audience: list[str] = Field(default_factory=list)
    estimated_duration_s: float | None = None
    requirements: Requirements
    parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    assets: dict[str, Asset] = Field(default_factory=dict)
    procedure: list[Step]
    notes: str = ""

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self.parameters,
            "additionalProperties": False,
        }

    @model_validator(mode="after")
    def _check_references(self) -> ExperimentDefinition:
        Draft202012Validator.check_schema(self.parameters_schema)
        roles = self.requirements.tools
        for step in self.procedure:
            role, _, member = (step.show if isinstance(step, ShowStep) else step.run).partition(".")
            if role not in roles:
                raise ValueError(f"procedure refers to unknown role {role!r}")
            needed = roles[role].streams if isinstance(step, ShowStep) else roles[role].actions
            if member not in needed:
                kind = "stream" if isinstance(step, ShowStep) else "action"
                raise ValueError(f"{kind} {member!r} must be listed in requirements for {role!r}")
            if isinstance(step, RunStep):
                for value in step.input.values():
                    self._check_expr(value)
        for asset in self.assets.values():
            if isinstance(asset, GeneratedAsset):
                for value in asset.generate.values():
                    self._check_expr(value)
        return self

    def _check_expr(self, value: Any) -> None:
        if not (isinstance(value, str) and "{{" in value):
            return
        m = _EXPR.match(value)
        if m is None:
            raise ValueError(f"unsupported expression {value!r}")
        if (p := m["param"] or m["indirect"]) and p not in self.parameters:
            raise ValueError(f"unknown parameter in {value!r}")
        if (a := m["asset"]) and a not in self.assets:
            raise ValueError(f"unknown asset in {value!r}")


def resolve(value: Any, params: dict[str, Any], assets: dict[str, str]) -> Any:
    if not (isinstance(value, str) and "{{" in value):
        return value
    m = _EXPR.match(value)
    assert m is not None  # checked when the definition was loaded
    if m["param"]:
        return params[m["param"]]
    if m["asset"]:
        return assets[m["asset"]]
    key = str(params[m["indirect"]])
    if key not in assets:
        raise ApiError(422, "invalid_parameters", f"No asset named {key!r}")
    return assets[key]


# --------------------------------------------------------------------------- #
# API models
# --------------------------------------------------------------------------- #


class CompatibleWorkbench(BaseModel):
    workbench_id: str
    bindings: dict[str, str]


class ExperimentOut(ExperimentDefinition):
    model_config = ConfigDict(extra="ignore")
    compatible_workbenches: list[CompatibleWorkbench]


class ExperimentRunCreate(BaseModel):
    lease_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ExperimentRunOut(BaseModel):
    id: str
    experiment_id: str
    experiment_version: int
    workbench_id: str
    lease_id: str
    parameter_values: dict[str, Any]
    bindings: dict[str, str]
    state: RunState
    error: ErrorInfo | None
    created_at: datetime
    finished_at: datetime | None
    runs: list[RunOut]


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


def bind_roles(
    definition: ExperimentDefinition, bench: WorkbenchDescriptor
) -> dict[str, str] | None:
    """Map each role to a distinct tool on the bench that satisfies it, or None."""
    bindings: dict[str, str] = {}
    used: set[str] = set()
    for role, req in definition.requirements.tools.items():
        match = next(
            (
                t
                for t in bench.tools
                if t.name not in used
                and t.type == req.type
                and all(t.action(a) for a in req.actions)
                and all(t.stream(st) for st in req.streams)
            ),
            None,
        )
        if match is None:
            return None
        bindings[role] = match.name
        used.add(match.name)
    return bindings


class ExperimentService:
    def __init__(self) -> None:
        self._definitions: dict[str, ExperimentDefinition] = {}
        # experiment id -> asset name -> lab-owned file id (static assets only)
        self._asset_files: dict[str, dict[str, str]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_lease: dict[str, str] = {}
        self._services: Services | None = None

    def bind(self, services: Services) -> None:
        self._services = services

    @property
    def s(self) -> Services:
        assert self._services is not None
        return self._services

    # ---------------------------------------------------------------- catalog

    async def load_directory(self, root: Path) -> None:
        paths = await asyncio.to_thread(lambda: sorted(root.glob("*/experiment.yaml")))
        if not paths:
            log.warning("No experiments found in %s", root)
        for path in paths:
            try:
                await self.load_file(path)
            except (ValidationError, ValueError, OSError, yaml.YAMLError) as exc:
                log.error("Skipping invalid experiment %s: %s", path, exc)

    async def load_file(self, path: Path) -> ExperimentDefinition:
        text = await asyncio.to_thread(path.read_text)
        definition = ExperimentDefinition.model_validate(yaml.safe_load(text))
        folder = await asyncio.to_thread(path.parent.resolve)
        files: dict[str, str] = {}
        for name, asset in definition.assets.items():
            if isinstance(asset, FileAsset):
                asset_path = await asyncio.to_thread((folder / asset.file).resolve)
                if folder not in asset_path.parents:
                    raise ValueError(f"asset {name!r} points outside the experiment folder")
                files[name] = await self._lab_file(asset_path)
        self._definitions[definition.id] = definition
        self._asset_files[definition.id] = files
        log.info("Loaded experiment %s v%d", definition.id, definition.version)
        return definition

    async def _lab_file(self, path: Path) -> str:
        data = await asyncio.to_thread(path.read_bytes)
        sha = hashlib.sha256(data).hexdigest()
        async with self.s.db.session() as s:
            existing = await s.execute(
                select(File.id).where(
                    File.owner_id.is_(None), File.sha256 == sha, File.filename == path.name
                )
            )
            found = existing.scalar_one_or_none()
        if found is not None:
            return found
        return (await self.s.files.save_bytes(None, path.name, data)).id

    def definition(self, experiment_id: str) -> ExperimentDefinition:
        definition = self._definitions.get(experiment_id)
        if definition is None:
            raise not_found("experiment", experiment_id)
        return definition

    def to_out(self, definition: ExperimentDefinition) -> ExperimentOut:
        compatible = []
        for workbench_id in self.s.agents.online_workbenches():
            descriptor = self.s.agents.descriptor(workbench_id)
            if descriptor and (b := bind_roles(definition, descriptor)) is not None:
                compatible.append(CompatibleWorkbench(workbench_id=workbench_id, bindings=b))
        return ExperimentOut(**definition.model_dump(), compatible_workbenches=compatible)

    def list(self) -> list[ExperimentOut]:
        return [self.to_out(d) for d in sorted(self._definitions.values(), key=lambda d: d.id)]

    # ------------------------------------------------------------------- runs

    async def start(
        self, principal: Principal, experiment_id: str, body: ExperimentRunCreate
    ) -> ExperimentRunOut:
        definition = self.definition(experiment_id)

        validator = Draft202012Validator(definition.parameters_schema)
        errors = list(validator.iter_errors(body.parameters))
        if errors:
            raise ApiError(
                422,
                "invalid_parameters",
                "Parameters do not match the experiment",
                [{"path": list(e.path), "message": e.message} for e in errors],
            )
        params = {
            name: body.parameters.get(name, spec.get("default"))
            for name, spec in definition.parameters.items()
        }
        missing = [n for n, v in params.items() if v is None]
        if missing:
            raise ApiError(422, "invalid_parameters", f"Missing parameters: {', '.join(missing)}")

        async with self.s.db.session() as s:
            lease = await s.get(Lease, body.lease_id)
            if lease is None or lease.principal_id != principal.id:
                raise ApiError(403, "lease_required", "You need an active lease")
            if lease.state != LeaseState.ACTIVE:
                raise ApiError(409, "lease_not_active", f"Lease is {lease.state}")
            busy = await s.execute(
                select(ExperimentRun.id).where(
                    ExperimentRun.lease_id == lease.id,
                    ExperimentRun.state.in_([RunState.PENDING, RunState.RUNNING]),
                )
            )
            if busy.first() is not None:
                raise ApiError(409, "experiment_busy", "An experiment is already running")

            descriptor = self.s.agents.descriptor(lease.workbench_id)
            if descriptor is None:
                raise ApiError(409, "workbench_offline", "The workbench is offline")
            bindings = bind_roles(definition, descriptor)
            if bindings is None:
                raise ApiError(
                    409, "incompatible_workbench", "This workbench lacks the required tools"
                )

            xrun = ExperimentRun(
                id=new_id("xrun"),
                experiment_id=definition.id,
                experiment_version=definition.version,
                workbench_id=lease.workbench_id,
                lease_id=lease.id,
                principal_id=principal.id,
                parameter_values=params,
                bindings=bindings,
                state=RunState.PENDING,
                created_at=utcnow(),
            )
            s.add(xrun)
            await s.commit()

        task = asyncio.create_task(self._execute(principal, definition, xrun.id), name=xrun.id)
        self._tasks[xrun.id] = task
        self._task_lease[xrun.id] = xrun.lease_id
        task.add_done_callback(lambda _t, xid=xrun.id: self._forget(xid))
        out = await self.get_out(xrun.id)
        self._publish(principal.id, out)
        return out

    def _forget(self, xrun_id: str) -> None:
        self._tasks.pop(xrun_id, None)
        self._task_lease.pop(xrun_id, None)

    async def _execute(
        self, principal: Principal, definition: ExperimentDefinition, xrun_id: str
    ) -> None:
        state, error = RunState.SUCCEEDED, None
        try:
            await self._set_state(principal.id, xrun_id, RunState.RUNNING)
            async with self.s.db.session() as s:
                xrun = await s.get(ExperimentRun, xrun_id)
                assert xrun is not None
            params = xrun.parameter_values
            assets = dict(self._asset_files.get(definition.id, {}))
            for name, asset in definition.assets.items():
                if isinstance(asset, GeneratedAsset):
                    assets[name] = await self._generate(principal, definition, name, asset, params)

            for step in definition.procedure:
                if not isinstance(step, RunStep):
                    continue  # "show" steps are display hints for clients
                role, _, action = step.run.partition(".")
                run_input = {k: resolve(v, params, assets) for k, v in step.input.items()}
                run = await self.s.runs.create(
                    principal,
                    xrun.workbench_id,
                    xrun.bindings[role],
                    action,
                    xrun.lease_id,
                    run_input,
                    experiment_run_id=xrun_id,
                )
                self._publish(principal.id, await self.get_out(xrun_id))
                finished = await self.s.runs.wait(run.id)
                if finished.state != RunState.SUCCEEDED:
                    state = (
                        RunState.CANCELLED
                        if finished.state == RunState.CANCELLED
                        else RunState.FAILED
                    )
                    error = finished.error or ErrorInfo(code="step_failed", message="A step failed")
                    break
        except asyncio.CancelledError:
            state, error = RunState.CANCELLED, ErrorInfo(code="cancelled", message="Cancelled")
        except ApiError as exc:
            state, error = RunState.FAILED, ErrorInfo(code=exc.code, message=exc.message)
        except Exception as exc:
            log.exception("Experiment run %s crashed", xrun_id)
            state, error = RunState.FAILED, ErrorInfo(code="internal_error", message=str(exc))
        await self._set_state(principal.id, xrun_id, state, error)

    async def _generate(
        self,
        principal: Principal,
        definition: ExperimentDefinition,
        name: str,
        asset: GeneratedAsset,
        params: dict[str, Any],
    ) -> str:
        spec_values = {k: resolve(v, params, {}) for k, v in asset.generate.items()}
        try:
            spec = SineSpec.model_validate(spec_values)
        except ValidationError as exc:
            raise ApiError(422, "invalid_parameters", f"Cannot generate {name}: {exc}") from exc
        data = await asyncio.to_thread(sine_wav, spec)
        filename = f"{definition.id}-{name}-{spec.frequency_hz:g}hz.wav"
        return (await self.s.files.save_bytes(principal.id, filename, data)).id

    async def _set_state(
        self, principal_id: str, xrun_id: str, state: RunState, error: ErrorInfo | None = None
    ) -> None:
        async with self.s.db.session() as s:
            xrun = await s.get(ExperimentRun, xrun_id)
            assert xrun is not None
            xrun.state = state
            if state.terminal:
                xrun.finished_at = utcnow()
                xrun.error = error.model_dump() if error else None
            await s.commit()
        self._publish(principal_id, await self.get_out(xrun_id))

    async def cancel(self, principal: Principal, xrun_id: str) -> ExperimentRunOut:
        out = await self.get(principal, xrun_id)
        if out.state.terminal:
            return out
        for run in out.runs:
            if not run.state.terminal:
                await self.s.runs.request_cancel(run.workbench_id, run.id)
        task = self._tasks.get(xrun_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return await self.get(principal, xrun_id)

    async def on_lease_ended(self, lease_id: str) -> None:
        """Called by the lease manager; the agent resets the tools, we stop the procedure."""
        for xrun_id, lid in list(self._task_lease.items()):
            if lid == lease_id and (task := self._tasks.get(xrun_id)) is not None:
                task.cancel()

    async def recover_after_restart(self) -> None:
        async with self.s.db.session() as s:
            result = await s.execute(
                select(ExperimentRun).where(
                    ExperimentRun.state.in_([RunState.PENDING, RunState.RUNNING])
                )
            )
            for xrun in result.scalars():
                xrun.state = RunState.FAILED
                xrun.finished_at = utcnow()
                xrun.error = {"code": "gateway_restarted", "message": "The gateway restarted"}
            await s.commit()

    # ------------------------------------------------------------------ reads

    async def get_out(self, xrun_id: str) -> ExperimentRunOut:
        async with self.s.db.session() as s:
            xrun = await s.get(ExperimentRun, xrun_id)
            if xrun is None:
                raise not_found("experiment_run", xrun_id)
            runs = await s.execute(
                select(Run).where(Run.experiment_run_id == xrun_id).order_by(Run.created_at)
            )
            return ExperimentRunOut(
                id=xrun.id,
                experiment_id=xrun.experiment_id,
                experiment_version=xrun.experiment_version,
                workbench_id=xrun.workbench_id,
                lease_id=xrun.lease_id,
                parameter_values=xrun.parameter_values,
                bindings=xrun.bindings,
                state=RunState(xrun.state),
                error=ErrorInfo.model_validate(xrun.error) if xrun.error else None,
                created_at=xrun.created_at,
                finished_at=xrun.finished_at,
                runs=[run_out(r) for r in runs.scalars()],
            )

    async def get(self, principal: Principal, xrun_id: str) -> ExperimentRunOut:
        async with self.s.db.session() as s:
            xrun = await s.get(ExperimentRun, xrun_id)
        if xrun is None or xrun.principal_id != principal.id:
            raise not_found("experiment_run", xrun_id)
        return await self.get_out(xrun_id)

    async def list_for(self, principal: Principal, limit: int = 50) -> list[ExperimentRunOut]:
        async with self.s.db.session() as s:
            result = await s.execute(
                select(ExperimentRun.id)
                .where(ExperimentRun.principal_id == principal.id)
                .order_by(ExperimentRun.created_at.desc())
                .limit(limit)
            )
            ids = list(result.scalars())
        return [await self.get_out(i) for i in ids]

    def _publish(self, principal_id: str, out: ExperimentRunOut) -> None:
        self.s.bus.publish("experiment_run.updated", out.model_dump(mode="json"), to=principal_id)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

ServicesDep = Annotated[Services, Depends(get_services)]
router = APIRouter(tags=["experiments"])


@router.get("/v1/experiments")
async def list_experiments(services: ServicesDep) -> list[ExperimentOut]:
    return services.experiments.list()


@router.get("/v1/experiments/{experiment_id}")
async def get_experiment(experiment_id: str, services: ServicesDep) -> ExperimentOut:
    return services.experiments.to_out(services.experiments.definition(experiment_id))


@router.post("/v1/experiments/{experiment_id}/runs", status_code=202)
async def start_experiment(
    experiment_id: str,
    body: ExperimentRunCreate,
    principal: CurrentPrincipal,
    services: ServicesDep,
) -> ExperimentRunOut:
    """Run an experiment on the workbench you hold a lease for."""
    return await services.experiments.start(principal, experiment_id, body)


@router.get("/v1/experiment-runs/{xrun_id}")
async def get_experiment_run(
    xrun_id: str, principal: CurrentPrincipal, services: ServicesDep
) -> ExperimentRunOut:
    return await services.experiments.get(principal, xrun_id)


@router.post("/v1/experiment-runs/{xrun_id}/cancel")
async def cancel_experiment_run(
    xrun_id: str, principal: CurrentPrincipal, services: ServicesDep
) -> ExperimentRunOut:
    return await services.experiments.cancel(principal, xrun_id)


@router.get("/v1/me/experiment-runs", tags=["library"])
async def list_my_experiment_runs(
    principal: CurrentPrincipal, services: ServicesDep
) -> list[ExperimentRunOut]:
    return await services.experiments.list_for(principal)
