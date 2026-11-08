"""An AI agent drives the lab over MCP: lease, generate a tone, play it, look at the camera."""

from __future__ import annotations

import json
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from tests.conftest import WORKBENCH, Lab


def _data(result: Any) -> Any:
    assert not result.is_error, result.content
    if result.structured_content is not None:
        content = result.structured_content
        # Tools returning lists are wrapped as {"result": [...]}.
        return content.get("result", content) if set(content) == {"result"} else content
    return json.loads(_text(result))


def _text(result: Any) -> str:
    [content] = result.content
    assert isinstance(content, TextContent)
    return content.text


async def test_agent_runs_the_lab_over_mcp(lab: Lab) -> None:
    session = (await lab.client.post("/v1/sessions", json={"display_name": "Agent"})).json()
    headers = {"Authorization": f"Bearer {session['token']}"}
    async with (
        httpx2.AsyncClient(headers=headers) as http,
        Client(streamable_http_client(f"{lab.url}/mcp", http_client=http)) as client,
    ):
        tools = {t.name for t in (await client.list_tools()).tools}
        assert {"list_workbenches", "request_lease", "run_action", "get_camera_frame"} <= tools

        [bench] = _data(await client.call_tool("list_workbenches", {}))
        assert bench["id"] == WORKBENCH

        lease = _data(await client.call_tool("request_lease", {"workbench_id": WORKBENCH}))
        assert lease["state"] == "active"

        tone = _data(await client.call_tool("create_tone", {"frequency_hz": 45, "duration_s": 0.3}))
        run = _data(
            await client.call_tool(
                "run_action",
                {
                    "workbench_id": WORKBENCH,
                    "tool": "vibrator",
                    "action": "play_waveform",
                    "lease_id": lease["id"],
                    "input": {"audio": tone["id"], "gain": 0.3},
                },
            )
        )
        assert run["state"] == "succeeded", run

        frame = await client.call_tool("get_camera_frame", {"workbench_id": WORKBENCH})
        assert not frame.is_error
        [image] = frame.content
        assert image.type == "image"
        assert image.mime_type == "image/jpeg"

        released = _data(await client.call_tool("release_lease", {"lease_id": lease["id"]}))
        assert released["state"] == "released"

        # Errors come back as tool errors with the API's stable code.
        result = await client.call_tool(
            "run_action",
            {
                "workbench_id": WORKBENCH,
                "tool": "vibrator",
                "action": "play_waveform",
                "lease_id": lease["id"],
                "input": {"audio": tone["id"]},
            },
        )
        assert result.is_error
        assert "lease_not_active" in _text(result)


async def test_mcp_requires_a_session(lab: Lab) -> None:
    async with Client(f"{lab.url}/mcp") as client:
        result = await client.call_tool("list_workbenches", {})
        assert result.is_error
        assert "authentication_required" in _text(result)


async def test_agent_without_headers_signs_in_with_start_session(lab: Lab) -> None:
    """Most MCP clients cannot set headers: the agent gets a token from a tool instead."""
    async with Client(f"{lab.url}/mcp") as client:
        # The error says exactly what to do next.
        denied = await client.call_tool("list_workbenches", {})
        assert denied.is_error
        assert "start_session" in _text(denied) and "session_token" in _text(denied)

        session = _data(await client.call_tool("start_session", {"display_name": "Claude"}))
        token = session["token"]
        assert token.startswith("cbt_") and session["display_name"] == "Claude"

        [bench] = _data(await client.call_tool("list_workbenches", {"session_token": token}))
        assert bench["id"] == WORKBENCH
        args = {"workbench_id": WORKBENCH, "session_token": token}
        lease = _data(await client.call_tool("request_lease", args))
        assert lease["state"] == "active"
        assert lease["holder"] == "Claude"
        released = _data(
            await client.call_tool(
                "release_lease", {"lease_id": lease["id"], "session_token": token}
            )
        )
        assert released["state"] == "released"

        bad = await client.call_tool("list_workbenches", {"session_token": "cbt_nope"})
        assert bad.is_error
        assert "invalid_token" in _text(bad) and "start_session" in _text(bad)

        empty = await client.call_tool("start_session", {"display_name": ""})
        assert empty.is_error


async def test_mcp_instructions_explain_signing_in(lab: Lab) -> None:
    async with Client(f"{lab.url}/mcp") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "start_session" in tools
        schema = tools["list_workbenches"].input_schema
        assert "start_session" in schema["properties"]["session_token"]["description"]
        assert "session_token" not in schema.get("required", [])
