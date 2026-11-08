"""The hackathon demo reset page: a secret URL that clears the bench."""

from __future__ import annotations

import pytest

from tests.conftest import WORKBENCH, Lab, eventually
from tests.test_e2e import lease

TOKEN = "demo-0123456789abcdef0123456789"


@pytest.mark.parametrize("gateway_overrides", [{"demo_reset_token": TOKEN}])
async def test_demo_reset_ends_the_session_and_clears_the_queue(lab: Lab) -> None:
    alice, bob, carol = await lab.guest("Alice"), await lab.guest("Bob"), await lab.guest("Carol")
    a, b, c = await lease(alice), await lease(bob), await lease(carol)
    assert (a["state"], b["state"], c["state"]) == ("active", "queued", "queued")

    page = await lab.client.get(f"/demo-reset/{TOKEN}")
    assert page.status_code == 200
    assert "End session and clear queue" in page.text

    r = await lab.client.post(f"/demo-reset/{TOKEN}")
    assert r.status_code == 200
    assert r.json() == {"holders": 1, "queued": 2}
    for client, held in ((alice, a), (bob, b), (carol, c)):
        out = (await client.get(f"/v1/leases/{held['id']}")).json()
        assert (out["state"], out["end_reason"]) == ("revoked", "cleared_for_demo")

    # The bench is free once it has reset, so the presenter gets it straight away.
    async def free() -> bool:
        return (await lab.client.get(f"/v1/workbenches/{WORKBENCH}")).json()["available"]

    await eventually(free)
    presenter = await lab.guest("Presenter")
    assert (await lease(presenter))["state"] == "active"

    # Nothing left to clear.
    r = await lab.client.post(f"/demo-reset/{TOKEN}")
    assert r.json() == {"holders": 1, "queued": 0}


@pytest.mark.parametrize("gateway_overrides", [{"demo_reset_token": TOKEN}])
async def test_demo_reset_needs_the_exact_token(lab: Lab) -> None:
    alice = await lab.guest()
    a = await lease(alice)
    for path in ("/demo-reset/wrong", f"/demo-reset/{TOKEN}x", f"/demo-reset/{TOKEN[:-1]}"):
        assert (await lab.client.get(path)).status_code == 404
        assert (await lab.client.post(path)).status_code == 404
    assert (await alice.get(f"/v1/leases/{a['id']}")).json()["state"] == "active"


async def test_demo_reset_is_off_by_default(lab: Lab) -> None:
    assert (await lab.client.get(f"/demo-reset/{TOKEN}")).status_code == 404
    assert (await lab.client.post(f"/demo-reset/{TOKEN}")).status_code == 404
