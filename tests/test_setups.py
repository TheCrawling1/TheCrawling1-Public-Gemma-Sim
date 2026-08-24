"""Scenario setups / scene-staging."""
import pytest

from app import create_app
from app import entities, setups


@pytest.fixture(scope="module")
def world():
    app = create_app()
    with app.app_context():
        ents = entities.load_all()
        return ents, ents["closing_time_marginalia"]


def test_sample_scenario_has_setups(world):
    _, scenario = world
    ids = [s["id"] for s in scenario["setups"]]
    assert ids == ["closing_together", "first_time", "dex_deadline", "scene_staging"]


def test_scene_staging_gate(world):
    """scene_staging_fields returns fields only for the staging setup."""
    _, scenario = world
    by_id = {s["id"]: s for s in scenario["setups"]}
    assert setups.scene_staging_fields(by_id["closing_together"]) is None
    fields = setups.scene_staging_fields(by_id["scene_staging"])
    assert fields is not None
    assert "characters" in fields and "locations" in fields


def test_scene_staging_refs_resolve(world):
    """Every character/location the staging panel offers really exists."""
    ents, scenario = world
    staging = next(s for s in scenario["setups"] if s["id"] == "scene_staging")
    fields = staging["scene_staging_fields"]
    for cid in fields["characters"]:
        assert cid in ents, f"staging character missing: {cid}"
    for lid in fields["locations"]:
        assert lid in ents, f"staging location missing: {lid}"


def test_user_personas_present(world):
    _, scenario = world
    role_ids = {p["id"] for p in scenario["user_personas"]}
    assert {"regular", "newcomer"} <= role_ids
    assert scenario.get("user_personas_are_roles") is True
