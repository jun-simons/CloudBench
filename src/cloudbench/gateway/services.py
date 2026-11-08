"""The gateway's long-lived components, created once per app and shared by routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.requests import HTTPConnection

from cloudbench.gateway.config import GatewaySettings
from cloudbench.gateway.db import Database
from cloudbench.gateway.events import EventBus, Presence

if TYPE_CHECKING:
    from cloudbench.gateway.agents import AgentHub
    from cloudbench.gateway.experiments import ExperimentService
    from cloudbench.gateway.files import FileStore
    from cloudbench.gateway.leases import LeaseManager
    from cloudbench.gateway.runs import RunService
    from cloudbench.gateway.streams import StreamHub


@dataclass(eq=False)
class Services:
    settings: GatewaySettings
    db: Database
    bus: EventBus
    presence: Presence
    files: FileStore
    agents: AgentHub
    leases: LeaseManager
    runs: RunService
    streams: StreamHub
    experiments: ExperimentService


def get_services(conn: HTTPConnection) -> Services:
    return conn.app.state.services
