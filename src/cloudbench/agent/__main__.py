from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

from cloudbench.agent.audio import AudioOutput, MockOutput, SimulatedSurface, SoundDeviceOutput
from cloudbench.agent.autocrop import with_autocrop
from cloudbench.agent.camera import Camera, ffmpeg_source, mock_source, opencv_source
from cloudbench.agent.config import AgentConfig
from cloudbench.agent.runtime import Agent
from cloudbench.agent.tools import CameraTool, VibratorTool
from cloudbench.protocol import WorkbenchDescriptor


def build_agent(config: AgentConfig, secret: str) -> Agent:
    surface = SimulatedSurface()
    output: AudioOutput = (
        SoundDeviceOutput(config.vibrator.device, config.vibrator.max_gain)
        if config.vibrator.driver == "sounddevice"
        else MockOutput(surface)
    )
    if config.camera.driver == "opencv":
        source = opencv_source(config.camera)
    elif config.camera.driver == "ffmpeg":
        source = ffmpeg_source(config.camera)
    else:
        source = mock_source(config.camera, surface)
    if config.camera.auto_crop:
        source = with_autocrop(source, config.camera)
    camera = Camera(config.camera, source)
    vibrator = VibratorTool(config.vibrator, output)
    camera_tool = CameraTool(config.camera, camera, config.vision)
    workbench = WorkbenchDescriptor(
        id=config.workbench_id,
        name=config.name,
        description=config.description,
        tools=[vibrator.descriptor, camera_tool.descriptor],
    )
    return Agent(
        config.gateway_url,
        workbench,
        secret,
        tools=[vibrator, camera_tool],
        cameras={("camera", "video"): camera, **camera_tool.overlay_streams()},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="CloudBench workbench agent")
    parser.add_argument("--config", type=Path, help="agent YAML config (see agent.example.yaml)")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    config = AgentConfig.load(args.config)
    secret = os.environ.get("CLOUDBENCH_AGENT_SECRET") or config.secret
    if not secret:
        sys.exit("Set CLOUDBENCH_AGENT_SECRET (or `secret` in the config file)")
    agent = build_agent(config, secret)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(agent.run())


if __name__ == "__main__":
    main()
