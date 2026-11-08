"""Shared types between the gateway and workbench agents.

Two things live here:

* Descriptors: how an agent describes its workbench, tools, actions and streams.
  The gateway stores them and serves them unchanged in the public API, so they
  double as the public shape of those resources.
* Agent protocol messages: the JSON messages sent over the control WebSocket
  between the gateway and a workbench agent. Every message has a ``type`` field.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

PROTOCOL_VERSION = 1

# JSON Schema "format" used in action input schemas for parameters that take the
# ID of an uploaded File. The gateway checks the file exists and resolves it to a
# signed download URL before forwarding the run to the agent.
FILE_ID_FORMAT = "file-id"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Descriptors
# --------------------------------------------------------------------------- #


class ActionDescriptor(_Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    title: str
    description: str
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    output_schema: dict[str, Any] | None = None
    limits: dict[str, Any] = Field(default_factory=dict)
    safety: dict[str, Any] = Field(default_factory=dict)
    # True for quick control actions (e.g. adjust or stop a running tone) that may be
    # invoked while another run on the same tool is in progress.
    concurrent: bool = False


class StreamKind(StrEnum):
    MJPEG = "mjpeg"


class StreamAccess(StrEnum):
    PUBLIC = "public"
    LEASE = "lease"


class StreamDescriptor(_Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: StreamKind = StreamKind.MJPEG
    access: StreamAccess = StreamAccess.PUBLIC
    width: int | None = None
    height: int | None = None
    fps: float | None = None


class ToolDescriptor(_Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    type: str
    title: str
    description: str = ""
    actions: list[ActionDescriptor] = Field(default_factory=list)
    streams: list[StreamDescriptor] = Field(default_factory=list)

    def action(self, name: str) -> ActionDescriptor | None:
        return next((a for a in self.actions if a.name == name), None)

    def stream(self, name: str) -> StreamDescriptor | None:
        return next((s for s in self.streams if s.name == name), None)


class WorkbenchDescriptor(_Model):
    id: str = Field(pattern=r"^wb_[a-z0-9_]{1,60}$")
    name: str
    description: str = ""
    tools: list[ToolDescriptor] = Field(default_factory=list)

    def tool(self, name: str) -> ToolDescriptor | None:
        return next((t for t in self.tools if t.name == name), None)


# --------------------------------------------------------------------------- #
# Run states (shared because the agent reports them)
# --------------------------------------------------------------------------- #


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED)


class ToolStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    FAULT = "fault"


class ErrorInfo(_Model):
    code: str
    message: str


# --------------------------------------------------------------------------- #
# Agent -> gateway
# --------------------------------------------------------------------------- #


class Hello(_Model):
    type: Literal["hello"] = "hello"
    protocol_version: int = PROTOCOL_VERSION
    agent_version: str
    workbench: WorkbenchDescriptor


class Heartbeat(_Model):
    type: Literal["heartbeat"] = "heartbeat"
    tools: dict[str, ToolStatus] = Field(default_factory=dict)


class RunUpdate(_Model):
    type: Literal["run.update"] = "run.update"
    run_id: str
    state: RunState
    output: dict[str, Any] | None = None
    error: ErrorInfo | None = None


class ResetDone(_Model):
    type: Literal["reset.done"] = "reset.done"
    lease_id: str


class DescriptorChanged(_Model):
    type: Literal["descriptor.changed"] = "descriptor.changed"
    workbench: WorkbenchDescriptor


AgentMessage = Annotated[
    Hello | Heartbeat | RunUpdate | ResetDone | DescriptorChanged,
    Field(discriminator="type"),
]
agent_message_adapter: TypeAdapter[AgentMessage] = TypeAdapter(AgentMessage)


# --------------------------------------------------------------------------- #
# Gateway -> agent
# --------------------------------------------------------------------------- #


class Welcome(_Model):
    type: Literal["welcome"] = "welcome"
    heartbeat_interval_s: float


class RunStart(_Model):
    type: Literal["run.start"] = "run.start"
    run_id: str
    lease_id: str
    tool: str
    action: str
    input: dict[str, Any]
    # file_id -> short-lived URL (absolute or relative to the gateway base URL)
    files: dict[str, str] = Field(default_factory=dict)


class RunCancel(_Model):
    type: Literal["run.cancel"] = "run.cancel"
    run_id: str


class LeaseEnded(_Model):
    type: Literal["lease.ended"] = "lease.ended"
    lease_id: str


GatewayMessage = Annotated[
    Welcome | RunStart | RunCancel | LeaseEnded,
    Field(discriminator="type"),
]
gateway_message_adapter: TypeAdapter[GatewayMessage] = TypeAdapter(GatewayMessage)


# --------------------------------------------------------------------------- #
# Internal endpoint paths shared by both sides
# --------------------------------------------------------------------------- #

AGENT_CONTROL_PATH = "/internal/agents/control"
AGENT_STREAM_PATH = "/internal/agents/streams/{tool}/{stream}"
