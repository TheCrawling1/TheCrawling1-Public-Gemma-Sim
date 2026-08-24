"""B-side inheritance.

A "B-side" is an alternate version of a character — same person, different
proportions / styling / personality. By convention its id is the A-side id
plus a ``_bside`` suffix (``iris`` -> ``iris_bside``); a character may also
declare its A-side explicitly via ``properties.bside_of``.

B-sides are stored as fully independent character entities. To avoid making
authors re-declare a B-side's whole wardrobe and image pack, this module lets
a B-side *inherit* its A-side's outfits and image/sprite config, while still
allowing per-field overrides:

  - **Outfits** merge: a B-side offers its A-side's outfits *plus* any outfits
    unique to the B-side (see ``merged_outfit_ids`` / ``owner_aliases``).
  - **Images** fall back: a B-side with no image config of its own renders
    using the A-side's ``images`` / ``sprite_id`` / ``image_pack`` /
    ``image_packs`` (see ``image_view``). A B-side that declares its own image
    config keeps it untouched.

Linking is resolved from the entity alone (id suffix or ``bside_of``); the
A-side entity is looked up from the global registry, so inheritance works even
when the A-side isn't part of the current scene.
"""
from __future__ import annotations

from typing import Any

_SUFFIX = "_bside"


def a_side_id(character: dict[str, Any] | str | None) -> str | None:
    """Return the A-side character id for a B-side, or None.

    Accepts a character entity or a bare id. Prefers an explicit
    ``properties.bside_of``; otherwise strips a ``_bside`` suffix from the
    entity id (or the template id it was instanced from). Returns the
    *candidate* id without verifying it exists — callers that need the
    A-side entity get None back from the lookup when it doesn't.
    """
    if isinstance(character, dict):
        props = character.get("properties") or {}
        explicit = props.get("bside_of")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        candidates = [character.get("id"), character.get("_template_id")]
    else:
        candidates = [character]
    for cand in candidates:
        if isinstance(cand, str) and cand.endswith(_SUFFIX) and len(cand) > len(_SUFFIX):
            return cand[: -len(_SUFFIX)]
    return None


def _a_side_entity(
    character: dict[str, Any], entities: dict[str, dict[str, Any]] | None
) -> dict[str, Any] | None:
    """Resolve the A-side entity from the given map, falling back to the
    global registry so inheritance works off-scene."""
    aid = a_side_id(character)
    if not aid:
        return None
    a = (entities or {}).get(aid)
    if a is None:
        from . import entities as ent  # lazy import avoids a cycle
        a = ent.get(aid)
    return a if isinstance(a, dict) else None


def owner_aliases(
    character: dict[str, Any], entities: dict[str, dict[str, Any]] | None = None
) -> set[str]:
    """Lowercased owner ids whose outfits this character should surface.

    Always includes the character itself; for a B-side it also includes the
    A-side, so A-side-owned outfits appear in the B-side's wardrobe.
    """
    out: set[str] = set()
    cid = character.get("id")
    if isinstance(cid, str):
        out.add(cid.lower())
    aid = a_side_id(character)
    if aid:
        out.add(aid.lower())
    return out


def merged_outfit_ids(
    character: dict[str, Any], entities: dict[str, dict[str, Any]] | None = None
) -> list[str]:
    """Outfit ids linked to a character via ``properties.outfits``, with the
    A-side's linked outfits merged in first for a B-side. Order: A-side's
    outfits, then the B-side's own; deduplicated, preserving order."""
    ids: list[str] = []
    a = _a_side_entity(character, entities)
    if a is not None:
        for oid in (a.get("properties") or {}).get("outfits") or []:
            if isinstance(oid, str):
                ids.append(oid)
    for oid in (character.get("properties") or {}).get("outfits") or []:
        if isinstance(oid, str):
            ids.append(oid)
    seen: set[str] = set()
    deduped: list[str] = []
    for oid in ids:
        if oid not in seen:
            seen.add(oid)
            deduped.append(oid)
    return deduped


# Image-catalog property keys a B-side inherits wholesale from its A-side when
# it declares no image config of its own. Branch-scoped runtime fields
# (enabled_image_packs, base_images_enabled, current_outfit_profile, …) are
# deliberately NOT here — those stay the B-side's own per-conversation state.
_IMAGE_CONFIG_KEYS = ("images", "sprite_id", "image_pack", "image_packs")


def image_view(
    character: dict[str, Any] | None,
    entities: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return the character to use for image/sprite resolution.

    If `character` is a B-side with no image config of its own, returns a
    shallow copy whose properties carry the A-side's image config (so the
    B-side renders with the A-side's sprite / image pack). Otherwise returns
    `character` unchanged — a B-side that overrides its own images keeps them.
    Non-mutating.
    """
    if not character:
        return character
    from .sprite_url import image_format  # lazy import avoids a cycle
    if image_format(character) is not None:
        return character  # has its own image config — nothing to inherit
    a = _a_side_entity(character, entities)
    if a is None or image_format(a) is None:
        return character
    aprops = a.get("properties") or {}
    props = dict(character.get("properties") or {})
    for key in _IMAGE_CONFIG_KEYS:
        if key in aprops:
            props[key] = aprops[key]
    merged = dict(character)
    merged["properties"] = props
    return merged
