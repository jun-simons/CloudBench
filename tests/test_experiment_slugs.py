"""Experiment slugs: the experiment's address in the web app."""

from __future__ import annotations

from pathlib import Path

import pytest

from cloudbench.gateway.experiments import ExperimentService
from tests.conftest import REPO

FARADAY = REPO / "experiments" / "faraday-waves" / "experiment.yaml"


async def test_experiment_slugs_must_be_unique(tmp_path: Path) -> None:
    service = ExperimentService()
    loaded = await service.load_file(FARADAY)
    assert loaded.slug == "faraday-wave"

    dupe = tmp_path / "dupe" / "experiment.yaml"
    dupe.parent.mkdir()
    dupe.write_text(FARADAY.read_text().replace("id: exp_faraday_waves", "id: exp_faraday_copy"))
    with pytest.raises(
        ValueError, match="slug 'faraday-wave' is already used by exp_faraday_waves"
    ):
        await service.load_file(dupe)

    # Reloading the same experiment keeps its own slug.
    assert (await service.load_file(FARADAY)).slug == "faraday-wave"
