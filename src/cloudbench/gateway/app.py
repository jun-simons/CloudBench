from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cloudbench import __version__
from cloudbench.gateway import agents, api, demo, experiments, files, streams, workbenches
from cloudbench.gateway.agents import AgentHub
from cloudbench.gateway.config import GatewaySettings
from cloudbench.gateway.db import Database
from cloudbench.gateway.errors import install_error_handlers
from cloudbench.gateway.events import EventBus, Presence
from cloudbench.gateway.experiments import ExperimentService
from cloudbench.gateway.files import FileStore
from cloudbench.gateway.leases import LeaseManager
from cloudbench.gateway.mcp import build_mcp_app, build_mcp_server
from cloudbench.gateway.runs import RunService
from cloudbench.gateway.services import Services
from cloudbench.gateway.streams import StreamHub

log = logging.getLogger(__name__)


def build_services(settings: GatewaySettings) -> Services:
    bus = EventBus()
    services = Services(
        settings=settings,
        db=Database(settings.database_path),
        bus=bus,
        presence=Presence(bus),
        files=FileStore(settings),
        agents=AgentHub(),
        leases=LeaseManager(),
        runs=RunService(),
        streams=StreamHub(),
        experiments=ExperimentService(),
    )
    for component in (
        services.files,
        services.agents,
        services.leases,
        services.runs,
        services.experiments,
    ):
        component.bind(services)
    return services


async def _tick_loop(services: Services) -> None:
    while True:
        await asyncio.sleep(services.settings.tick_interval_s)
        try:
            await services.agents.check_timeouts()
            await services.leases.tick()
        except Exception:
            log.exception("Tick failed")


def create_app(settings: GatewaySettings | None = None) -> FastAPI:
    settings = settings or GatewaySettings()
    services = build_services(settings)
    mcp_server = build_mcp_server(services)
    mcp_app = build_mcp_app(mcp_server)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await services.db.create_schema()
        await services.leases.recover_after_restart()
        await services.runs.fail_inflight(None, "gateway_restarted", "The gateway restarted")
        await services.experiments.recover_after_restart()
        await services.experiments.load_directory(settings.experiments_dir)
        tick = asyncio.create_task(_tick_loop(services), name="gateway-tick")
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)
            await services.agents.close_all()
            await services.db.close()

    app = FastAPI(
        title="CloudBench",
        version=__version__,
        description="Lease real lab hardware, run experiments, watch the results live.",
        lifespan=lifespan,
    )
    app.state.services = services
    install_error_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    for module in (api, workbenches, files, streams, experiments, agents, demo):
        app.include_router(module.router)
    # MCP over Streamable HTTP at /mcp, for AI agents.
    app.router.routes.extend(mcp_app.routes)

    if settings.web_dir is not None:
        _mount_web(app, settings.web_dir)
    return app


def _mount_web(app: FastAPI, web_dir: Path) -> None:
    """Serve the built single-page app, falling back to index.html for client routes."""
    index = web_dir / "index.html"
    if not index.is_file():
        log.warning("Web dir %s has no index.html; not serving the web app", web_dir)
        return
    assets = web_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    # Top-level files of the build (favicon, manifest, ...), fixed at startup.
    top_level = {p.name: p for p in web_dir.iterdir() if p.is_file()}

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if (found := top_level.get(path)) is not None and path != "index.html":
            return FileResponse(found)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
