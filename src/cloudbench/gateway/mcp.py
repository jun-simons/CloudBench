"""MCP endpoint for AI agents: a thin adapter over the same services as the REST API.

Agents authenticate with a session token, like the web app. Many MCP clients cannot
set HTTP headers (and the model inside one never can), so there are two ways: the
client sends ``Authorization: Bearer <token>`` (from ``POST /v1/sessions``), or the
agent calls the ``start_session`` tool and passes the token it returns as the
``session_token`` argument of every other tool.
Actions are not turned into one MCP tool each; ``list_workbenches`` returns
every action's JSON Schema and ``run_action`` invokes any of them. That keeps
the tool list stable while workbenches come and go.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
from typing import Annotated, Any

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, ValidationError
from sqlalchemy import select
from starlette.applications import Starlette

from cloudbench.gateway.auth import SessionCreate, create_session, principal_for_token
from cloudbench.gateway.db import Principal, Workbench
from cloudbench.gateway.errors import ApiError
from cloudbench.gateway.experiments import ExperimentRunCreate
from cloudbench.gateway.files import file_out
from cloudbench.gateway.services import Services
from cloudbench.gateway.streams import check_stream_access, stream_descriptor
from cloudbench.gateway.waveforms import ToneSpec, tone_wav
from cloudbench.gateway.workbenches import workbench_summary
from cloudbench.signals import Shape

INSTRUCTIONS = """\
CloudBench lets you run experiments on real lab hardware and observe the physical result.

Authentication comes first. Every tool except start_session needs a session token:
- If your MCP client was configured with an "Authorization: Bearer <token>" header, you are
  already signed in; leave session_token out.
- Otherwise call start_session once with a display name (people see it on the bench), then
  pass the returned token as the session_token argument of every other tool call. Reuse the
  same token for the whole conversation: leases and files belong to it.

Typical flow:
0. start_session (unless your client sends a token for you).
1. list_workbenches: see benches, their tools, each action's input_schema, and streams.
2. request_lease on a workbench. If the lease is "queued", poll get_lease; when it becomes
   "offered", call accept_lease. Only an "active" lease can run actions.
3. Either run_experiment (a ready-made recipe from list_experiments) or run_action directly.
   For a live tone use the vibrator's play_tone (with a short duration_s, or stop it with the
   stop action); set_tone adjusts it while it plays. Clip actions take a file ID: make one
   with create_tone or upload_wav.
4. get_camera_frame to look at the result.
5. release_lease when done so the next person can use the bench.

Leases expire after a few minutes, and are released if you make no calls for about a minute.
"""

_MAX_WAIT_S = 120.0

SessionTokenArg = Annotated[
    str | None,
    Field(
        description="Token from start_session. Leave out only if your MCP client sends an "
        "Authorization: Bearer header."
    ),
]


def _wrap(exc: ApiError) -> ToolError:
    return ToolError(f"{exc.code}: {exc.message}")


def build_mcp_server(services: Services) -> MCPServer:
    server = MCPServer(name="CloudBench", instructions=INSTRUCTIONS)

    async def caller(ctx: Context, session_token: str | None) -> Principal:
        headers = ctx.headers or {}
        scheme, _, header_token = (headers.get("authorization") or "").partition(" ")
        token = (session_token or "").strip()
        if not token and scheme.lower() == "bearer":
            token = header_token.strip()
        if not token:
            raise ToolError(
                "authentication_required: call the start_session tool with a display name, "
                "then pass the token it returns as session_token to this and every other tool."
            )
        principal = await principal_for_token(services, token)
        if principal is None:
            raise ToolError(
                "invalid_token: that session token is unknown or has expired. Call start_session "
                "for a new one and use it as session_token."
            )
        return principal

    async def wait(run_id: str, timeout_s: float) -> None:
        # On timeout, return the current state; the caller can poll get_run.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(services.runs.wait(run_id), min(timeout_s, _MAX_WAIT_S))

    # -------------------------------------------------------------- session

    @server.tool()
    async def start_session(display_name: str) -> dict[str, Any]:
        """Sign in as a guest. Call this first unless your MCP client already sends an
        Authorization header. Pass the returned "token" as session_token to every other tool.

        display_name is shown to people watching the bench (1-40 characters), e.g. "Claude".
        """
        try:
            body = SessionCreate(display_name=display_name)
        except ValidationError as exc:
            raise ToolError("invalid_input: display_name must be 1-40 characters") from exc
        session = await create_session(services, body.display_name)
        return {
            "token": session.token,
            "display_name": session.principal.display_name,
            "next": "Pass this token as session_token to every other tool, e.g. "
            'list_workbenches(session_token="<token>").',
        }

    # ------------------------------------------------------------ discovery

    @server.tool()
    async def list_workbenches(
        ctx: Context, session_token: SessionTokenArg = None
    ) -> list[dict[str, Any]]:
        """List workbenches with status, queue length, tools, action input schemas and streams."""
        await caller(ctx, session_token)
        async with services.db.session() as s:
            rows = (await s.execute(select(Workbench).order_by(Workbench.id))).scalars().all()
            return [
                (await workbench_summary(services, s, row)).model_dump(mode="json") for row in rows
            ]

    @server.tool()
    async def list_experiments(
        ctx: Context, session_token: SessionTokenArg = None
    ) -> list[dict[str, Any]]:
        """List ready-made experiments, their parameters (JSON Schema) and compatible benches."""
        await caller(ctx, session_token)
        return [e.model_dump(mode="json") for e in services.experiments.list()]

    # --------------------------------------------------------------- leases

    @server.tool()
    async def request_lease(
        ctx: Context, workbench_id: str, session_token: SessionTokenArg = None
    ) -> dict[str, Any]:
        """Join a workbench's queue. Returns your lease: "active" (go ahead) or "queued"."""
        principal = await caller(ctx, session_token)
        try:
            lease, _ = await services.leases.request(principal, workbench_id)
        except ApiError as exc:
            raise _wrap(exc) from exc
        return lease.model_dump(mode="json")

    @server.tool()
    async def get_lease(
        ctx: Context, lease_id: str, session_token: SessionTokenArg = None
    ) -> dict[str, Any]:
        """Current state of one of your leases, including queue position and expiry."""
        principal = await caller(ctx, session_token)
        try:
            async with services.db.session() as s:
                lease = await services.leases.get(s, lease_id)
                if lease.principal_id != principal.id:
                    raise ApiError(404, "lease_not_found", f"Lease {lease_id!r} not found")
                return (await services.leases.to_out(s, lease)).model_dump(mode="json")
        except ApiError as exc:
            raise _wrap(exc) from exc

    @server.tool()
    async def accept_lease(
        ctx: Context, lease_id: str, session_token: SessionTokenArg = None
    ) -> dict[str, Any]:
        """Accept an "offered" lease to make it "active"."""
        principal = await caller(ctx, session_token)
        try:
            return (await services.leases.accept(principal, lease_id)).model_dump(mode="json")
        except ApiError as exc:
            raise _wrap(exc) from exc

    @server.tool()
    async def release_lease(
        ctx: Context, lease_id: str, session_token: SessionTokenArg = None
    ) -> dict[str, Any]:
        """Release the workbench (or leave the queue). Always do this when you are done."""
        principal = await caller(ctx, session_token)
        try:
            return (await services.leases.release(principal, lease_id)).model_dump(mode="json")
        except ApiError as exc:
            raise _wrap(exc) from exc

    # ---------------------------------------------------------------- files

    @server.tool()
    async def create_tone(
        ctx: Context,
        frequency_hz: float,
        duration_s: float,
        amplitude: float = 1.0,
        shape: Shape = Shape.SINE,
        session_token: SessionTokenArg = None,
    ) -> dict[str, Any]:
        """Generate a mono WAV tone (sine, square, triangle or sawtooth) in your library and
        return it (use its "id"). For live, adjustable tones use run_action with play_tone."""
        principal = await caller(ctx, session_token)
        try:
            spec = ToneSpec(
                frequency_hz=frequency_hz, duration_s=duration_s, amplitude=amplitude, shape=shape
            )
        except ValidationError as exc:
            raise ToolError(f"invalid_input: {exc}") from exc
        data = await asyncio.to_thread(tone_wav, spec)
        filename = f"tone-{shape.value}-{frequency_hz:g}hz.wav"
        f = await services.files.save_bytes(principal.id, filename, data)
        return file_out(f).model_dump(mode="json")

    @server.tool()
    async def upload_wav(
        ctx: Context,
        base64_data: str,
        filename: str = "upload.wav",
        session_token: SessionTokenArg = None,
    ) -> dict[str, Any]:
        """Upload a WAV file (base64-encoded) to your library and return it (use its "id")."""
        principal = await caller(ctx, session_token)
        try:
            data = base64.b64decode(base64_data, validate=True)
        except binascii.Error as exc:
            raise ToolError("invalid_input: base64_data is not valid base64") from exc
        if len(data) > services.settings.max_upload_bytes:
            raise ToolError("file_too_large: the file is over the upload limit")
        try:
            f = await services.files.save_bytes(principal.id, filename, data, "audio/wav")
        except ApiError as exc:
            raise _wrap(exc) from exc
        return file_out(f).model_dump(mode="json")

    # ----------------------------------------------------------------- runs

    @server.tool()
    async def run_action(
        ctx: Context,
        workbench_id: str,
        tool: str,
        action: str,
        lease_id: str,
        input: dict[str, Any] | None = None,
        wait_s: float = 60,
        session_token: SessionTokenArg = None,
    ) -> dict[str, Any]:
        """Invoke a tool action on a workbench you hold an active lease for.

        ``input`` must match the action's input_schema from list_workbenches. Waits up to
        ``wait_s`` seconds for the run to finish and returns it; if it is still running,
        poll get_run.
        """
        principal = await caller(ctx, session_token)
        try:
            run = await services.runs.create(
                principal, workbench_id, tool, action, lease_id, input or {}
            )
            await wait(run.id, wait_s)
            return (await services.runs.get(principal, run.id)).model_dump(mode="json")
        except ApiError as exc:
            raise _wrap(exc) from exc

    @server.tool()
    async def get_run(
        ctx: Context, run_id: str, session_token: SessionTokenArg = None
    ) -> dict[str, Any]:
        """State, output and error of one of your runs."""
        principal = await caller(ctx, session_token)
        try:
            return (await services.runs.get(principal, run_id)).model_dump(mode="json")
        except ApiError as exc:
            raise _wrap(exc) from exc

    @server.tool()
    async def cancel_run(
        ctx: Context, run_id: str, session_token: SessionTokenArg = None
    ) -> dict[str, Any]:
        """Stop a running action."""
        principal = await caller(ctx, session_token)
        try:
            return (await services.runs.cancel(principal, run_id)).model_dump(mode="json")
        except ApiError as exc:
            raise _wrap(exc) from exc

    @server.tool()
    async def run_experiment(
        ctx: Context,
        experiment_id: str,
        lease_id: str,
        parameters: dict[str, Any] | None = None,
        wait_s: float = 60,
        session_token: SessionTokenArg = None,
    ) -> dict[str, Any]:
        """Run a ready-made experiment on the workbench you hold an active lease for.

        Omitted parameters use their defaults. Waits up to ``wait_s`` seconds.
        """
        principal = await caller(ctx, session_token)
        try:
            xrun = await services.experiments.start(
                principal,
                experiment_id,
                ExperimentRunCreate(lease_id=lease_id, parameters=parameters or {}),
            )
            deadline = asyncio.get_running_loop().time() + min(wait_s, _MAX_WAIT_S)
            while not xrun.state.terminal and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.25)
                xrun = await services.experiments.get(principal, xrun.id)
            return xrun.model_dump(mode="json")
        except ApiError as exc:
            raise _wrap(exc) from exc

    # --------------------------------------------------------------- camera

    @server.tool()
    async def get_camera_frame(
        ctx: Context,
        workbench_id: str,
        tool: str = "camera",
        stream: str = "video",
        session_token: SessionTokenArg = None,
    ) -> Image:
        """The latest camera frame as a JPEG image, to see what is happening on the bench."""
        principal = await caller(ctx, session_token)
        try:
            descriptor = stream_descriptor(services, workbench_id, tool, stream)
            await check_stream_access(services, principal, workbench_id, descriptor)
        except ApiError as exc:
            raise _wrap(exc) from exc
        frame = services.streams.channel(workbench_id, tool, stream).frame
        if frame is None:
            raise ToolError("no_frame: no frame received from the camera yet")
        return Image(data=frame, format="jpeg")

    return server


def build_mcp_app(server: MCPServer) -> Starlette:
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # Every call carries a bearer token checked against the database, so the
        # localhost-only DNS rebinding guard does not apply; the gateway is public.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
