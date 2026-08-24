"""Narrator directive grammar parsing."""
from app.narrator import extract_edits


def test_plain_prose_has_no_edits():
    prose = "Iris looks up from the counter and smiles."
    cleaned, edits = extract_edits(prose)
    assert edits == []
    assert cleaned == prose


def test_move_directive():
    text = "[move iris_calloway -> marginalia_counter]\n\nShe steps behind the counter."
    cleaned, edits = extract_edits(text)
    assert edits == [
        {"kind": "move", "character_id": "iris_calloway", "room": "marginalia_counter"}
    ]
    # the directive line is stripped from the visible prose
    assert "[move" not in cleaned
    assert "She steps behind the counter." in cleaned


def test_move_with_location_and_room():
    _, edits = extract_edits("[move dex_okafor -> the_marginalia:marginalia_nook]")
    assert edits == [
        {
            "kind": "move",
            "character_id": "dex_okafor",
            "location": "the_marginalia",
            "room": "marginalia_nook",
        }
    ]


def test_outfit_directive():
    _, edits = extract_edits("[outfit iris_calloway -> iris_casual]")
    assert edits == [
        {"kind": "outfit", "character_id": "iris_calloway", "outfit_id": "iris_casual"}
    ]


def test_multiple_directives_in_order():
    text = (
        "[move iris_calloway -> marginalia_counter]\n"
        "[move dex_okafor -> marginalia_nook]\n\n"
        "The bell rattles as the door opens."
    )
    _, edits = extract_edits(text)
    assert [e["character_id"] for e in edits] == ["iris_calloway", "dex_okafor"]
    assert all(e["kind"] == "move" for e in edits)
