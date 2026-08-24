"""Clothing v2 — slot-based composition module.

Pure-data functions for the v2 clothing system documented in
``docs/clothing.md``. NOT YET WIRED INTO PRODUCTION — `personas.py`
and `api.py` continue to use v1 ``current_outfit`` + ``coverage``
paths. This module ships ahead of step 4 (dual-read renderer) so
the verify script can exercise the v2 data shape end-to-end before
we touch the live renderer.

Step 4 will land the dual-read by importing from this module —
`_body_description` checks `character.properties.worn` first;
if present, calls `compose_body_description_v2`; otherwise falls
back to the v1 path. Same shape for `_resolve_sprite_state`.

Note on field name: the character's slot-map field is named
`properties.worn` (NOT `properties.equipped`) because
`properties.equipped` is already in use as a list of
equipped-object ids (accessories, held items, etc.) read by
`personas._equipped_text`. Using `worn` for the clothing slot
map lets v2 coexist with the existing object-equip path cleanly.
"""
from __future__ import annotations

from typing import Any


# Layer order for body-part composition. Top of the list wins when
# multiple worn pieces cover the same body part. Matches the
# default-layer-order section in docs/clothing.md.
#
# Reading: an overlay (tattoo) wins over a face piece (mask) which
# wins over a head piece (hat), which wins over a top (shirt), and so
# on down to the under-layers (bra, underwear, phallus). Pieces in a
# slot not listed here are appended last (lowest priority).
#
# Slot naming notes:
# - `underwear` (formerly `panties`) is gender-neutral and covers
#   the underwear layer (briefs / boxers / trunks and similar).
#   Display name per piece comes from the `noun` field, not the
#   slot id.
# - `phallus` is an optional under-layer slot that sits BENEATH
#   underwear on characters that define it — a piece worn on that
#   layer with underwear layered over the top normally. Only
#   meaningful on characters that use it; empty on others.
LAYER_ORDER: tuple[str, ...] = (
    "overlay",
    "face",
    "head",
    "top",
    "bottom",
    "legwear",
    "shoes",
    "gloves",
    "pantyhose",
    "bra",
    "underwear",
    "phallus",
    "neck",
    "back",
)

# Sprite-aligned slots in the order the sprite compositor expects.
# See `app/sprite_compose.py` — the 8-tuple of (top, bottom, bra,
# underwear, pantyhose, gloves, legwear, shoes) feeds compose_png.
# `phallus` is not in the sprite 8-tuple — no PNG layer exists for
# it in the current image catalog; it's a prose-only slot.
SPRITE_SLOT_ORDER: tuple[str, ...] = (
    "top", "bottom", "bra", "underwear",
    "pantyhose", "gloves", "legwear", "shoes",
)

# Legacy worn-slot keys → canonical name. Some character data (Serena)
# still files the underwear slot under the pre-rename key "panties"; the
# sprite resolver + prose composer key by SPRITE_SLOT_ORDER / LAYER_ORDER
# ("underwear"), so a raw "panties" key would never render or layer. Map
# it on resolve so legacy data works without a data migration. Mirrors the
# same alias already applied on the v1-override path.
_WORN_SLOT_ALIASES: dict[str, str] = {"panties": "underwear"}


def _resolve_piece(piece_id: str, entities: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Look up a clothing piece by id. Checks the branch's effective
    entities first (instanced into the conversation); falls back to
    the global template catalog when the piece isn't instanced — the
    same idiom v1's `_compose_accessories` uses for outfit lookups.

    Without this fallback, narrator edits like
    `[set iris.properties.worn = {...}]` reference piece ids that
    never get pulled into the conversation's instance dir (clothing
    pieces aren't part of any scenario's transitive child set today)
    and the renderer silently falls through to body.base for every
    part — Iris appears bare even with the full outfit equipped.
    """
    piece = entities.get(piece_id)
    if isinstance(piece, dict) and piece.get("type") == "clothing":
        return piece
    # Fall back to the global template catalog. Wrapped in a try so
    # the v2 module stays importable from non-app contexts (e.g., the
    # verify scripts that load entities directly).
    try:
        from . import entities as _ent_mod
        tmpl = _ent_mod.get(piece_id)
        if isinstance(tmpl, dict) and tmpl.get("type") == "clothing":
            return tmpl
    except Exception:
        pass
    return None


def _resolve_outfit_bundle(outfit_id: str, entities: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Look up a v2 outfit bundle by id. Same global-catalog fallback
    as `_resolve_piece` — outfit bundles aren't auto-instanced into
    every conversation either."""
    outfit = entities.get(outfit_id)
    if isinstance(outfit, dict) and outfit.get("type") == "outfit":
        return outfit
    try:
        from . import entities as _ent_mod
        tmpl = _ent_mod.get(outfit_id)
        if isinstance(tmpl, dict) and tmpl.get("type") == "outfit":
            return tmpl
    except Exception:
        pass
    return None


def _resolve_equipped_pieces(
    equipped: dict[str, Any],
    entities: dict[str, dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], str]]:
    """Walk ``character.properties.worn`` and return ``{slot:
    (piece_entity, state)}``. Unresolvable piece ids are silently
    dropped; off-state slots are dropped (the renderer treats them as
    unequipped). Slot with no `state` defaults to the piece's
    ``states[0]``.
    """
    out: dict[str, tuple[dict[str, Any], str]] = {}
    if not isinstance(equipped, dict):
        return out
    for slot, entry in equipped.items():
        if not isinstance(entry, dict):
            continue
        piece_id = entry.get("piece")
        if not isinstance(piece_id, str):
            continue
        piece = _resolve_piece(piece_id, entities)
        if not piece:
            continue
        states = (piece.get("properties") or {}).get("states") or []
        state = entry.get("state")
        if not isinstance(state, str) or not state:
            state = states[0] if states else "on"
        if state == "off":
            # Treated as unequipped for rendering purposes.
            continue
        slot_key = _WORN_SLOT_ALIASES.get(slot, slot) if isinstance(slot, str) else slot
        out[slot_key] = (piece, state)
    return out


def signature_for_outfit(
    char: dict[str, Any],
    outfit: dict[str, Any],
    entities: dict[str, dict[str, Any]],
) -> str | None:
    """Return the outfit's ``signature_description`` when the
    character's ``worn`` state EXACTLY matches the outfit's
    ``equips`` map AND every piece is in its default state
    (``states[0]``). Otherwise return None — caller falls back to
    per-part composition.

    This is the bleed-prevention rule from docs/clothing.md: the
    signature only prints when the character is wearing the full
    intended look. Any deviation (piece swapped, state changed, slot
    cleared, extra piece equipped) drops the signature so the v1
    "skirt mentioned when she's undressed" bug can't happen.
    """
    props = char.get("properties") or {}
    worn = props.get("worn") or {}
    if not isinstance(worn, dict):
        return None
    outfit_props = outfit.get("properties") or {}
    equips = outfit_props.get("equips") or {}
    if not isinstance(equips, dict) or not equips:
        return None

    # Same slot set on both sides — strict membership check.
    if set(worn.keys()) != set(equips.keys()):
        return None

    # Each slot's piece must match AND state must be the piece's default.
    for slot, expected_piece_id in equips.items():
        entry = worn.get(slot) or {}
        if entry.get("piece") != expected_piece_id:
            return None
        piece = _resolve_piece(expected_piece_id, entities)
        if not piece:
            return None
        states = (piece.get("properties") or {}).get("states") or []
        default_state = states[0] if states else "on"
        actual_state = entry.get("state") or default_state
        if actual_state != default_state:
            return None

    sig = outfit_props.get("signature_description")
    return sig if isinstance(sig, str) and sig.strip() else None


def compose_body_description_v2(
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]],
) -> list[tuple[str, str]]:
    """Compose the per-body-part description using v2 worn data.

    Returns a list of ``(part_name, text)`` tuples in the character's
    ``concise_order`` (or insertion order). Caller joins for the
    prompt.

    Co-coverage composition rules. For each body part, walk equipped
    pieces in slot priority (LAYER_ORDER, top first). Behaviour
    depends on each piece's coverage entry:

      1. piece has ``covered: true, revealing: <unset/false>`` —
         this is the standard "occludes" case. Use this piece's
         description; stop walking. Lower-layer pieces (bra under
         shirt etc) are hidden.

      2. piece has ``covered: true, revealing: true`` — the garment
         is in the slot but is in a state that exposes what's
         underneath (shirt rolled up under the collarbones; skirt
         hiked to the waist). Append this piece's description and
         keep walking — the next lower piece's description (or the
         body's ``base`` if no covering piece below) also surfaces.
         The model sees both the garment-status detail AND the
         covering layer beneath.

      3. piece has ``covered: false`` — currently ignored (matches
         v1). Accessory contributions on uncovered parts are a
         future refinement.

      4. no piece touches this part — render the body's own
         ``base`` description (unclothed rendering).

    Without the revealing flag, rolling up a shirt + having a bra
    on dropped the bra entirely — the rolled-up shirt's covered=true
    pulled rank, the bra was never mentioned, model had no signal
    the bra existed. Authors mark partial states "revealing" when
    the garment is bunched/lifted/displaced away from the part.

    Pieces with empty coverage maps (e.g., `iris_white_ribbon`
    which is a presence-only slot anchor) contribute nothing here —
    body's `base` description handles head prose unchanged.
    """
    props = char.get("properties") or {}
    body_parts = props.get("body_parts") or {}
    if not isinstance(body_parts, dict):
        return []

    pieces = _resolve_equipped_pieces(props.get("worn") or {}, entities)

    # Sort pieces by LAYER_ORDER (top wins); unknown slots last.
    layer_rank = {slot: i for i, slot in enumerate(LAYER_ORDER)}
    ordered_slots = sorted(
        pieces.keys(),
        key=lambda s: layer_rank.get(s, len(LAYER_ORDER) + 1),
    )

    order = props.get("concise_order") or list(body_parts.keys())
    seen: set[str] = set()
    full_order: list[str] = []
    for k in order:
        if k in body_parts and k not in seen:
            full_order.append(k)
            seen.add(k)
    for k in body_parts:
        if k not in seen:
            full_order.append(k)
            seen.add(k)

    lines: list[tuple[str, str]] = []
    for part in full_order:
        bp = body_parts[part] or {}
        text_parts: list[str] = []
        saw_occluding = False
        for slot in ordered_slots:
            piece, state = pieces[slot]
            cov = ((piece.get("properties") or {}).get("coverage") or {}).get(state) or {}
            entry = cov.get(part)
            if not isinstance(entry, dict):
                continue
            if not entry.get("covered"):
                continue
            desc = entry.get("description") or ""
            if not isinstance(desc, str) or not desc.strip():
                continue
            text_parts.append(desc.strip())
            if not entry.get("revealing"):
                # Standard occlusion — this piece hides lower layers.
                saw_occluding = True
                break
            # else: revealing — keep walking lower layers

        if not text_parts:
            text = bp.get("base") or ""
        else:
            if not saw_occluding:
                # All layers were "revealing" — body.base also visible
                # below the bunched/lifted/displaced garments.
                base = (bp.get("base") or "").strip()
                if base:
                    text_parts.append(base)
            # Join co-covering fragments with a single space, normalising
            # trailing punctuation on each so the result reads as
            # consecutive sentences rather than a debug-style "a.; b"
            # seam. Earlier "; " joiner made the model treat the seam
            # as a structured field and skip it.
            normalised = []
            for part_text in text_parts:
                part_text = part_text.rstrip()
                if part_text and part_text[-1] not in ".!?":
                    part_text += "."
                normalised.append(part_text)
            text = " ".join(normalised)
        if text:
            lines.append((part, text))

    return lines


def resolve_sprite_slots_v2(
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]],
) -> tuple[tuple[int, ...], tuple[str | None, ...]]:
    """Build the 8-tuple of (slots, garments) the sprite compositor
    expects, from the character's ``worn`` map.

    Slot mapping uses `SPRITE_SLOT_ORDER`. For each slot:
      * No piece equipped → state 3, garment None.
      * Piece equipped → state is the positional index in the piece's
        `states` list (1=states[0], 2=states[1], 3=states[2]).
        Garment is the piece's `garment` field.
      * state "off" → state 3, garment None (handled by
        _resolve_equipped_pieces which drops off-state slots).

    Prose-only slots (head, neck, face, back, overlay) are ignored
    here — they don't contribute sprite layers. The character's base
    body PNG carries the head / face baseline imagery.
    """
    pieces = _resolve_equipped_pieces(
        (char.get("properties") or {}).get("worn") or {},
        entities,
    )
    slots_out: list[int] = []
    garments_out: list[str | None] = []
    for slot in SPRITE_SLOT_ORDER:
        entry = pieces.get(slot)
        if not entry:
            slots_out.append(3)
            garments_out.append(None)
            continue
        piece, state = entry
        states = (piece.get("properties") or {}).get("states") or []
        try:
            idx = states.index(state) + 1
        except ValueError:
            idx = 3
        # Clamp to the wardrobe.json range (1, 2, 3). Pieces with
        # more than 3 states would need a wardrobe.json extension to
        # render — capped here so a 4-state piece doesn't crash the
        # compositor; the prose still gets the full state vocabulary.
        if idx > 3:
            idx = 3
        slots_out.append(idx)
        garments_out.append(
            (piece.get("properties") or {}).get("garment") or "default"
        )
    return tuple(slots_out), tuple(garments_out)


def apply_outfit_preset_v2(
    char: dict[str, Any],
    outfit: dict[str, Any],
    entities: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Mutate ``char.properties.worn`` to match the preset's
    ``equips`` map, with each piece in its default state. Returns the
    new equipped dict for caller convenience.

    The default state is the piece's ``states[0]`` — NOT the literal
    string ``"on"``. Pieces whose first state isn't "on" (e.g. Samus's
    zero suit ``["intact","ripped","off"]``, a mask ``["down","up",
    "off"]``) would otherwise resolve to a coverage state that doesn't
    exist → the part falls through to bare ``base`` (unclothed prose) and the
    sprite renders every slot off. `entities` is needed to look up each
    piece's states; when omitted the state falls back to ``"on"`` (the
    legacy behaviour, correct only for on/off pieces).
    """
    outfit_props = outfit.get("properties") or {}
    equips = outfit_props.get("equips") or {}
    new_worn: dict[str, Any] = {}
    for slot, piece_id in equips.items():
        state = "on"
        if entities is not None:
            piece = _resolve_piece(piece_id, entities)
            states = (piece.get("properties") or {}).get("states") if piece else None
            if isinstance(states, list) and states and isinstance(states[0], str):
                state = states[0]
        new_worn[slot] = {"piece": piece_id, "state": state}
    char_props = char.setdefault("properties", {})
    char_props["worn"] = new_worn
    return new_worn


def apply_v1_overrides_to_worn(
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Translate v1 ``properties.clothing_overrides`` into v2
    ``properties.worn`` state changes. Returns a deep-copy of ``char``
    with the worn map updated; original ``char`` is unmodified.

    Used as a backcompat shim while the narrator prompt still teaches
    the v1 ``[set <char>.properties.clothing_overrides.<slot> = N]``
    directive. The narrator emits v1 directives; this function lets
    v2 characters honour them by mapping the integer slot-state
    semantically (not positionally) onto the piece's ``states`` list:

        clothing_overrides.<slot> = 1 → states[0]   (always "on/intact")
        clothing_overrides.<slot> = 2 → states[1]   IF the piece has
            3+ states (middle = partial-equivalent). For 2-state pieces
            (on/off only), the override is silently NO-OP — narrator's
            "partial" has no meaning for that piece.
        clothing_overrides.<slot> = 3 → states[-1]  (always "off")

    The semantic 1→first / 2→middle / 3→last mapping is more correct
    than naive positional (states[N-1]) because pieces with only
    ``[on, off]`` (most idol pieces, the bikini halves) would
    otherwise treat ``2`` as ``off`` — wrong, since the narrator means
    "partial". Skipping the override for 2-state pieces leaves the
    state at "on", which is the right default when no partial exists.

    No-op for v1 characters (no ``worn`` map) — they still go
    through the v1 ``clothing_overrides`` reader in api / personas.

    After step 7 collapses the narrator prompt, ``clothing_overrides``
    stops being emitted and this shim becomes dead code — safe to
    remove then.
    """
    import copy
    props = char.get("properties") or {}
    overrides = props.get("clothing_overrides")
    worn = props.get("worn")
    if not isinstance(overrides, dict) or not isinstance(worn, dict):
        return char
    if not overrides:
        return char

    new_char = copy.deepcopy(char)
    new_worn = new_char["properties"].get("worn") or {}
    if not isinstance(new_worn, dict):
        return char

    # Legacy slot-name aliases. The v1 `clothing_overrides` directive
    # used "panties" as the underwear-slot key; v2 renamed it to
    # "underwear" (gender-neutral). Map old keys to new so narrator
    # output emitted with the v1 vocabulary still lands.
    _LEGACY_SLOT_ALIASES = {"panties": "underwear"}

    for slot, value in overrides.items():
        if not isinstance(slot, str):
            continue
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if n not in (1, 2, 3):
            continue
        slot_key = _LEGACY_SLOT_ALIASES.get(slot.lower(), slot.lower())
        entry = new_worn.get(slot_key)
        if not isinstance(entry, dict):
            # Slot not in worn — nothing to translate. (Could
            # synthesize a worn entry from the piece, but the v1
            # override only makes sense when there's already a piece
            # to modify.)
            continue
        piece_id = entry.get("piece")
        piece = _resolve_piece(piece_id, entities) if piece_id else None
        if not piece:
            continue
        states = (piece.get("properties") or {}).get("states") or []
        if not states:
            continue
        # Semantic mapping (not positional):
        if n == 1:
            entry["state"] = states[0]
        elif n == 3:
            entry["state"] = states[-1]
        elif n == 2:
            # Middle state only meaningful when piece has 3+ states.
            # For 2-state pieces (on/off only), narrator's "partial"
            # has no piece-defined match — skip so state stays at
            # whatever it was (typically "on"). Authors add a middle
            # state to the piece's `states` list to enable partial.
            if len(states) >= 3:
                entry["state"] = states[1]
            # else: silently skip
    return new_char
