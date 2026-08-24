"""Entity loading + validation."""
import pytest

from app import create_app
from app import entities


@pytest.fixture(scope="module")
def loaded():
    app = create_app()
    with app.app_context():
        return entities.load_all()


def test_sample_entities_load(loaded):
    """The bundled sample world loads and is keyed by id."""
    for eid in (
        "iris_calloway",
        "dex_okafor",
        "closing_time_marginalia",
        "the_marginalia",
        "marginalia_floor",
        "marginalia_counter",
        "marginalia_nook",
    ):
        assert eid in loaded, f"missing sample entity: {eid}"


def test_sample_entity_types(loaded):
    assert loaded["iris_calloway"]["type"] == "character"
    assert loaded["closing_time_marginalia"]["type"] == "scenario"
    assert loaded["the_marginalia"]["type"] == "location"
    assert loaded["marginalia_nook"]["type"] == "room"


def test_sample_characters_use_sfw_format(loaded):
    """Both sample characters use the prose SFW schema (appearance +
    boundaries), and carry no explicit body_parts anatomy table."""
    for cid in ("iris_calloway", "dex_okafor"):
        props = loaded[cid].get("properties", {})
        assert isinstance(props.get("appearance"), dict) and props["appearance"]
        assert isinstance(props.get("boundaries"), str) and props["boundaries"].strip()
        assert "body_parts" not in props, f"{cid} should not define body_parts"


def test_validate_entity_accepts_valid_character():
    out = entities.validate_entity({"type": "character", "id": "sample_npc", "name": "Sample"})
    assert out["id"] == "sample_npc"
    # defaults filled in
    assert out["tags"] == [] and out["properties"] == {} and out["children"] == []


def test_validate_entity_rejects_bad_type():
    with pytest.raises(ValueError):
        entities.validate_entity({"type": "not_a_real_type", "id": "x"})


def test_validate_entity_rejects_unsafe_id():
    """Ids are interpolated into filesystem paths, so traversal must fail."""
    with pytest.raises(ValueError):
        entities.validate_entity({"type": "character", "id": "../escape"})
