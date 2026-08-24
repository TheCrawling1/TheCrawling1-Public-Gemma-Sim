"""Layered entity editor: template → scenario override → conversation instance.

Three storage layers, in increasing specificity:

  template   data/<type>/<id>/<id>.json or equivalent (the canonical entity)
  scenario   scenario.character_overrides[<id>]  +  scenario.custom_outfits
  instance   instances/<conversation_id>/entities/<id>.json (deep-merged)

The right-panel editor reads `effective_entity` (the merge result + a
per-leaf origin map for color cues) and writes via `save_at_layer` to the
appropriate layer. Editing in chat defaults to the instance layer; when
editing a scenario, defaults to scenario; standalone, defaults to template.

`origin_map` lets the UI tag every leaf with where its current value comes
from ("template" / "scenario" / "instance"). Leaves explicitly removed at
a higher layer (`UNSET_MARKER`) get origin "unset" so the editor renders
them in muted grey.
"""
from __future__ import annotations

import copy
import json
import os
import random
import re
from typing import Any

from flask import current_app

from . import entities as ent
from . import conversations as convs
from .merge import UNSET_MARKER, deep_merge, deep_merged


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


# Per-template pool aliases: when a derivative template (e.g.
# generic_nude_male) shares the same randomization domain as a
# parent (generic_male), it inherits the parent's pool instead of
# duplicating the file. Add new aliases here as needed.
_POOL_ALIASES: dict[str, str] = {
    "generic_nude_male": "generic_male",
    "generic_nude_female": "generic_female",
}


def _load_random_pool(template_id: str) -> dict[str, list[str]]:
    """Load `data/random_pools/<template_id>.json`. Returns the
    dict of `{field: [choices, ...]}` (excluding `_*` metadata
    fields), or empty dict when the file doesn't exist. The
    materialize layer uses this to fill `{{placeholder}}` tokens
    in generic templates at instance-creation time.

    Falls back via `_POOL_ALIASES` when the requested template has
    no dedicated pool file — keeps derivative templates DRY.
    """
    try:
        data_dir = current_app.config.get("data_dir") or "data"
    except RuntimeError:
        # No app context — best-effort default for offline tools.
        data_dir = "data"

    def _read(tid: str) -> dict | None:
        p = os.path.join(data_dir, "random_pools", f"{tid}.json")
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                blob = json.load(f)
        except Exception:
            return None
        return blob if isinstance(blob, dict) else None

    blob = _read(template_id)
    if blob is None:
        alias = _POOL_ALIASES.get(template_id)
        if alias:
            blob = _read(alias)
    if not isinstance(blob, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in blob.items():
        if k.startswith("_"):  # _help, _comment, etc.
            continue
        if isinstance(v, list) and all(isinstance(x, str) for x in v) and v:
            out[k] = v
    return out


def _pick_random(pool: dict[str, list[str]]) -> dict[str, str]:
    """One random.choice per field. Returns the picks map used as
    the substitution dictionary for placeholder fills."""
    return {k: random.choice(v) for k, v in pool.items() if v}


def _substitute_placeholders(node: Any, picks: dict[str, str]) -> Any:
    """Walk `node` recursively; for every string leaf, replace
    `{{key}}` tokens with `picks[key]`. Unknown placeholders are
    left intact (defensive — the template author may have used a
    placeholder not yet in the pool; better to surface it visually
    in the rendered prose than to silently strip).

    Returns the mutated node (also mutates in-place for dicts/lists).
    """
    if isinstance(node, str):
        def _sub(m: re.Match) -> str:
            key = m.group(1)
            return picks.get(key, m.group(0))
        return _PLACEHOLDER_RE.sub(_sub, node)
    if isinstance(node, dict):
        for k, v in list(node.items()):
            node[k] = _substitute_placeholders(v, picks)
        return node
    if isinstance(node, list):
        for i, v in enumerate(node):
            node[i] = _substitute_placeholders(v, picks)
        return node
    return node


LAYER_TEMPLATE = "template"
LAYER_SCENARIO = "scenario"
LAYER_INSTANCE = "instance"
ORIGIN_UNSET = "unset"


# ---------------------------------------------------------------------------
# Effective view
# ---------------------------------------------------------------------------


def effective_entity(
    entity_id: str,
    *,
    scenario_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the merged entity (template + optional scenario override +
    optional conversation instance) plus an `_origin` map showing where
    each leaf came from."""
    layers: list[tuple[str, dict[str, Any]]] = []

    template = ent.get(entity_id)
    if template is not None:
        layers.append((LAYER_TEMPLATE, template))
    elif scenario_id:
        # Scenario-only entity (e.g. a custom outfit) — start from the
        # scenario's authored data as the "template" baseline.
        scen = ent.get(scenario_id)
        if scen:
            for outfit in scen.get("custom_outfits") or []:
                if isinstance(outfit, dict) and outfit.get("id") == entity_id:
                    layers.append((LAYER_TEMPLATE, outfit))
                    break

    if scenario_id and template is not None:
        # Look for a character_override patch in the scenario.
        scen = ent.get(scenario_id)
        if scen:
            patch = (scen.get("character_overrides") or {}).get(entity_id)
            if isinstance(patch, dict):
                layers.append((LAYER_SCENARIO, patch))

    if conversation_id:
        inst = ent.load_instance_entity(conversation_id, entity_id)
        if inst is not None:
            # The instance file is itself a fully-merged copy (we deep-merge
            # at instancing time). To keep the per-leaf origin honest we
            # only attribute leaves that *differ* from the lower layers.
            base = _merge_layers(layers)
            diff = _diff_patch(base, inst)
            if diff:
                layers.append((LAYER_INSTANCE, diff))

    if not layers:
        return None

    merged = _merge_layers(layers)
    merged["_origin"] = _build_origin(layers)
    merged["_id"] = entity_id
    merged["_layers_present"] = [name for name, _ in layers]
    return merged


def _merge_layers(layers: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for _, data in layers:
        deep_merge(out, copy.deepcopy(data))
    return out


def _diff_patch(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Recursively compute the minimal patch that turns `base` into `target`,
    using UNSET_MARKER for keys present in base but missing in target."""
    out: dict[str, Any] = {}
    keys = set(base.keys()) | set(target.keys())
    for k in keys:
        if k.startswith("_"):
            continue
        if k not in target:
            out[k] = UNSET_MARKER
        elif k not in base:
            out[k] = copy.deepcopy(target[k])
        elif isinstance(base[k], dict) and isinstance(target[k], dict):
            sub = _diff_patch(base[k], target[k])
            if sub:
                out[k] = sub
        elif base[k] != target[k]:
            out[k] = copy.deepcopy(target[k])
    return out


def _build_origin(layers: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """For every leaf in the merged result, record which layer set it.
    Mirror of the merge: a higher layer wins. Returns a nested dict that
    matches the entity shape, but with leaves replaced by layer names."""
    out: dict[str, Any] = {}
    for layer_name, data in layers:
        _stamp_origin(out, data, layer_name)
    return out


def _stamp_origin(target: dict[str, Any], src: dict[str, Any], layer: str) -> None:
    for k, v in src.items():
        if v == UNSET_MARKER:
            target[k] = ORIGIN_UNSET
        elif isinstance(v, dict):
            if not isinstance(target.get(k), dict):
                target[k] = {}
            _stamp_origin(target[k], v, layer)
        else:
            target[k] = layer


# ---------------------------------------------------------------------------
# Save at layer
# ---------------------------------------------------------------------------


def save_at_layer(
    entity_id: str,
    new_value: dict[str, Any],
    *,
    layer: str,
    scenario_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Persist `new_value` at the requested layer.

    For the template layer: writes the whole entity to data/.
    For scenario: stores the diff (vs template) under
      scenario.character_overrides[entity_id], or appends/updates a custom
      outfit when the entity is a scenario-only outfit.
    For instance: writes the whole entity to instances/<cid>/entities/.

    Returns a fresh effective_entity for whatever (scenario, conversation)
    context was supplied so the client can re-render with updated origins.
    """
    new_value = dict(new_value)
    new_value.pop("_origin", None)
    new_value.pop("_layers_present", None)
    new_value.pop("_id", None)
    new_value["id"] = entity_id

    if layer == LAYER_TEMPLATE:
        ent.save(new_value)

    elif layer == LAYER_SCENARIO:
        if not scenario_id:
            raise ValueError("scenario_id required when saving to scenario layer.")
        scen = ent.get(scenario_id)
        if not scen or scen.get("type") != "scenario":
            raise ValueError(f"Scenario {scenario_id!r} not found.")
        # Compute the diff against the template so the override stays minimal.
        template = ent.get(entity_id)
        if template is not None:
            patch = _diff_patch(template, new_value)
            scen.setdefault("character_overrides", {})
            if patch:
                scen["character_overrides"][entity_id] = patch
            else:
                scen["character_overrides"].pop(entity_id, None)
        else:
            # Scenario-only entity (custom outfit): replace it in-place.
            customs = list(scen.get("custom_outfits") or [])
            replaced = False
            for i, o in enumerate(customs):
                if isinstance(o, dict) and o.get("id") == entity_id:
                    customs[i] = new_value
                    replaced = True
                    break
            if not replaced:
                customs.append(new_value)
            scen["custom_outfits"] = customs
        ent.save(scen)

    elif layer == LAYER_INSTANCE:
        if not conversation_id:
            raise ValueError("conversation_id required when saving to instance layer.")
        ent.save_instance_entity(conversation_id, new_value)

    else:
        raise ValueError(f"unknown layer {layer!r}")

    return effective_entity(
        entity_id, scenario_id=scenario_id, conversation_id=conversation_id
    ) or {}


# ---------------------------------------------------------------------------
# Cast add/remove
# ---------------------------------------------------------------------------


def _cast_field_for(entity_type: str | None) -> str | None:
    """Map an entity's type onto the scenario field that lists it.

    Characters live in ``scenario.characters[]``, objects in
    ``scenario.objects[]``. Returns None for types we don't track.
    """
    if entity_type == "character":
        return "characters"
    if entity_type == "object":
        return "objects"
    return None


def add_to_scenario_cast(scenario_id: str, entity_id: str) -> dict[str, Any]:
    scen = ent.get(scenario_id)
    if not scen or scen.get("type") != "scenario":
        raise ValueError(f"Scenario {scenario_id!r} not found.")
    template = ent.get(entity_id)
    if not template:
        raise ValueError(f"Entity {entity_id!r} not found.")
    field = _cast_field_for(template.get("type"))
    if not field:
        raise ValueError(
            f"Entity {entity_id!r} type {template.get('type')!r} can't be cast."
        )
    cast = list(scen.get(field) or [])
    if entity_id not in cast:
        cast.append(entity_id)
    scen[field] = cast
    ent.save(scen)
    return scen


def remove_from_scenario_cast(scenario_id: str, entity_id: str) -> dict[str, Any]:
    scen = ent.get(scenario_id)
    if not scen or scen.get("type") != "scenario":
        raise ValueError(f"Scenario {scenario_id!r} not found.")
    template = ent.get(entity_id)
    field = _cast_field_for((template or {}).get("type"))
    if not field:
        # Fall back to characters[] for legacy callers that pass a stale
        # character_id whose template is gone — keep the list-cleanup path.
        field = "characters"
    scen[field] = [c for c in (scen.get(field) or []) if c != entity_id]
    if field == "characters":
        if isinstance(scen.get("starting_state"), dict):
            scen["starting_state"].pop(entity_id, None)
        if isinstance(scen.get("first_messages"), dict):
            scen["first_messages"].pop(entity_id, None)
        if isinstance(scen.get("character_overrides"), dict):
            scen["character_overrides"].pop(entity_id, None)
    ent.save(scen)
    return scen


def instance_entity_into_conversation(
    conversation_id: str,
    entity_id: str,
) -> dict[str, Any] | None:
    """Pull a character / object / room template into this
    conversation's instance dir if it isn't there already. Idempotent,
    file-management only — does not touch the instance scenario's cast
    list, any message's presence_snapshot, or any applied_edits log.

    Used by the cast plumbing so cast membership can be expressed as a
    branch-scoped ``cast_add`` edit while the underlying template file
    is shared across branches (the disk pool — see effective.py).
    Rooms ride the same path for the "move to any room" affordance —
    the move endpoint instances a global-only room so the prompt's
    surroundings block can render it.
    """
    template = ent.get(entity_id)
    if not template:
        raise ValueError(f"Entity {entity_id!r} not found.")
    etype = template.get("type")
    if etype not in ("character", "object", "room"):
        raise ValueError(f"Entity type {etype!r} can't be added to cast.")
    if ent.load_instance_entity(conversation_id, entity_id) is None:
        instanced = copy.deepcopy(template)
        instanced["_template_id"] = entity_id
        ent.save_instance_entity(conversation_id, instanced)
    if etype == "character":
        _pull_owned_outfits(conversation_id, entity_id)
    return ent.load_instance_entity(conversation_id, entity_id)


def _last_cast_edit_kind(log: list[dict[str, Any]], entity_id: str) -> str | None:
    """The kind of the most recent cast_add / cast_remove entry for
    ``entity_id`` in an applied_edits log, or None when it has none."""
    for e in reversed(log):
        if not isinstance(e, dict):
            continue
        if e.get("kind") in ("cast_add", "cast_remove") and e.get("id") == entity_id:
            return e.get("kind")
    return None


def add_to_conversation_cast(
    conversation_id: str,
    entity_id: str,
    *,
    location_id: str | None = None,
    room_id: str | None = None,
) -> dict[str, Any]:
    """Branch-scoped cast add. Pulls the entity into the instance dir
    (file-management only, shared with siblings) and appends a
    ``cast_add`` edit to the active leaf's ``applied_edits`` so the
    branch's effective cast picks it up. For characters, also patches
    the active leaf's ``presence_snapshot`` so they show up in the
    current room without waiting for path-replay (the cast widget
    reads the leaf snapshot directly).

    The shared instance scenario's ``characters[]`` / ``objects[]`` is
    NOT mutated — that list is the conversation's birth-time baseline
    and stays fixed. Cast membership for any branch is the baseline
    plus path-replayed cast_add / cast_remove edits.
    """
    instance = instance_entity_into_conversation(conversation_id, entity_id)
    if instance is None:
        raise ValueError(f"Entity {entity_id!r} not found.")
    template = ent.get(entity_id) or {}
    etype = (instance or template).get("type")

    conv = convs.load_conversation(conversation_id)
    if conv:
        leaf_id = conv.get("active_path_leaf")
        leaf = conv["messages"].get(leaf_id) if leaf_id else None
        if leaf is not None:
            meta = leaf.setdefault("metadata", {})
            log = list(meta.get("applied_edits") or [])
            # Skip only when the LAST cast edit for this id is already a
            # cast_add (no-op double-add). A blanket "any cast_add in the
            # log" check would drop the re-add after an add → remove →
            # add sequence on the same leaf — replay would end on the
            # remove. Mirrors appendAppliedEditOnActiveLeaf client-side.
            if _last_cast_edit_kind(log, entity_id) != "cast_add":
                log.append({"kind": "cast_add", "ok": True, "id": entity_id})
                meta["applied_edits"] = log
            if etype == "character":
                snap = dict(leaf.get("presence_snapshot") or {})
                presence = dict(snap.get("presence") or {})
                entry = dict(presence.get(entity_id) or {})
                if location_id:
                    entry["location"] = location_id
                if room_id:
                    entry["room"] = room_id
                entry.setdefault("outfit", (template.get("properties") or {}).get("current_outfit"))
                presence[entity_id] = entry
                leaf["presence_snapshot"] = {**snap, "presence": presence}
            convs.save_conversation(conv)

    if etype == "character":
        _maybe_propagate_partner_after_cast_change(conversation_id)
    return instance


def add_created_entity_to_cast(
    conversation_id: str,
    entity: dict[str, Any],
    *,
    location_id: str | None = None,
    room_id: str | None = None,
) -> dict[str, Any]:
    """Declare a TEMP cast member from a freshly-built entity (not pulled from the
    template pool) — e.g. an NPC minted on the fly, like a bestiary monster with a
    pf1e sheet. Writes the instance entity, then the same branch-scoped ``cast_add``
    + presence patch ``add_to_conversation_cast`` uses, so it's present in the room
    on this branch and removable exactly like any other cast member (the removal now
    survives regeneration). Idempotent on the cast_add for a given id."""
    entity = dict(entity)
    entity.setdefault("type", "character")
    if not entity.get("id"):
        entity["id"] = ent.new_id()
    ent.save_instance_entity(conversation_id, entity)
    eid = entity["id"]
    etype = entity.get("type")

    conv = convs.load_conversation(conversation_id)
    if conv:
        leaf_id = conv.get("active_path_leaf")
        leaf = conv["messages"].get(leaf_id) if leaf_id else None
        if leaf is not None:
            meta = leaf.setdefault("metadata", {})
            log = list(meta.get("applied_edits") or [])
            if _last_cast_edit_kind(log, eid) != "cast_add":
                log.append({"kind": "cast_add", "ok": True, "id": eid})
                meta["applied_edits"] = log
            if etype == "character":
                snap = dict(leaf.get("presence_snapshot") or {})
                presence = dict(snap.get("presence") or {})
                entry = dict(presence.get(eid) or {})
                if location_id:
                    entry["location"] = location_id
                if room_id:
                    entry["room"] = room_id
                presence[eid] = entry
                leaf["presence_snapshot"] = {**snap, "presence": presence}
            convs.save_conversation(conv)
    return entity


def _pull_owned_outfits(conversation_id: str, char_id: str) -> None:
    """Copy every outfit owned by ``char_id`` (and its ``extends``
    chain) into the instance dir if not already present. Mirrors the
    loop in entities.create_instance_from_scenario so a freshly
    instanced character arrives with their full wardrobe — without
    this, ``[outfit rosa -> ...]`` on a later turn 404s on the
    outfit lookup the cast block + sprite resolver do.
    """
    all_entities = ent.load_all()
    for tmpl_id, tmpl in all_entities.items():
        if tmpl.get("type") != "outfit":
            continue
        owner = (tmpl.get("properties") or {}).get("owner")
        if owner != char_id:
            continue
        if ent.load_instance_entity(conversation_id, tmpl_id) is not None:
            continue
        outfit_copy = copy.deepcopy(tmpl)
        outfit_copy["_template_id"] = tmpl_id
        ent.save_instance_entity(conversation_id, outfit_copy)
        base_id = (tmpl.get("properties") or {}).get("extends")
        seen: set[str] = {tmpl_id}
        while isinstance(base_id, str) and base_id and base_id not in seen:
            seen.add(base_id)
            if ent.load_instance_entity(conversation_id, base_id) is not None:
                break
            base = all_entities.get(base_id)
            if not base or base.get("type") != "outfit":
                break
            base_copy = copy.deepcopy(base)
            base_copy["_template_id"] = base_id
            ent.save_instance_entity(conversation_id, base_copy)
            base_id = (base.get("properties") or {}).get("extends")


def auto_instance_character(
    conversation_id: str,
    char_id: str,
) -> bool:
    """Pull an off-cast character template into the conversation instance.

    Used by the narrator-add flow so a directive like
    ``[move rosa -> marginalia_floor]`` against a character whose
    template lives in ``data/characters/`` but who hasn't been added
    to this conversation yet still produces a fully-instanced entity.

    Returns True iff a new instance was created — False when she's
    already present, the id doesn't resolve to a character template,
    or the id is the special ``user`` entity.

    Cast membership for the branch is the *caller's* responsibility:
    when this returns True, ``narrator_apply._record_edit`` emits a
    synthetic ``cast_add`` log entry alongside the original edit so
    path-replay sees the new char in the branch's effective cast.
    The shared instance scenario's ``characters[]`` is NOT mutated.
    """
    if char_id == "user":
        return False
    if ent.load_instance_entity(conversation_id, char_id) is not None:
        return False
    template = ent.get(char_id)
    if not template or template.get("type") != "character":
        return False

    instanced = copy.deepcopy(template)
    instanced["_template_id"] = char_id
    ent.save_instance_entity(conversation_id, instanced)
    _pull_owned_outfits(conversation_id, char_id)
    return True


GENERIC_MALE_TEMPLATE_ID = "generic_male"


def materialize_from_generic(
    conversation_id: str,
    new_id: str,
    *,
    generic_template_id: str = GENERIC_MALE_TEMPLATE_ID,
) -> bool:
    """Materialise a new character instance under ``new_id`` from a
    designated generic template.

    Used by the narrator-add flow when the model emits a patch (or
    move) directive against an id that doesn't match any character
    template — the case where the user introduces a freshly-named
    off-cast character like "Iris has a brother named Jonah".
    Without a fallback the patch silently fails because there's no
    instance baseline; with this fallback the new id is created on
    the fly with the generic template's body / mannerisms / personality
    and subsequent ``[set <new_id>.name = ...]`` patches re-skin it.

    The materialised entity carries ``id = new_id`` and
    ``_template_id = generic_template_id`` so provenance is preserved
    for future revert / origin-map operations. Outfits owned by the
    generic template are pulled into the conversation instance
    alongside the entity, same as ``auto_instance_character``.

    Returns True iff a new instance was created — False when:
      - ``new_id`` is ``user`` or already a known character template
        (in which case the caller should be using
        ``auto_instance_character`` instead)
      - ``new_id`` is already instanced in this conversation
      - the generic template id doesn't resolve
    """
    if new_id == "user":
        return False
    if ent.load_instance_entity(conversation_id, new_id) is not None:
        return False
    if ent.get(new_id) is not None:
        # The id resolves to a real template; the caller should use
        # auto_instance_character for that path. Refuse so we don't
        # silently shadow a real character with the generic body.
        return False
    template = ent.get(generic_template_id)
    if not template or template.get("type") != "character":
        return False

    instanced = copy.deepcopy(template)
    instanced["id"] = new_id
    instanced["_template_id"] = generic_template_id

    # Deriving from a REAL character (e.g. a doll version of Serena) vs.
    # skinning a generic materialize template? The scrubs below —
    # description, name, tags — exist to strip generic-template meta-text
    # ("A neutral adult-male template...") and are WRONG for a real
    # source, whose description/name/tags are genuine character data to
    # keep. `character_creation_mode` gates whether the derive path is
    # reachable; here we just detect it from the source template.
    _src_tags = template.get("tags") or []
    is_generic = (
        generic_template_id.startswith("generic_")
        or "narrator-materialisable" in _src_tags
        or "template" in _src_tags
    )

    # Randomization pass — pure random.choice (no model). Fills any
    # `{{placeholder}}` tokens in template strings (head.base
    # "{{hair_color}} hair", etc.) with per-instance picks from
    # data/random_pools/<template_id>.json. The narrator never sees
    # the picks; any explicit `[set <id>.X = ...]` patch overrides
    # the picked value at path-replay time via the standard
    # deep-merge, so narrator intent always wins.
    pool = _load_random_pool(generic_template_id)
    picks = _pick_random(pool)
    if picks:
        _substitute_placeholders(instanced, picks)

    # Name: prefer the random pool pick when available, else fall
    # back to the id-derived label so the entity has a sane name even
    # without a pool file. `jake` -> `Jake`; `jake_smith` -> `Jake Smith`.
    # The narrator's explicit `[set <id>.name = ...]` rename patch
    # overrides at render time via deep-merge, so a "Jake Smith" patch
    # still wins. This is the safety floor — nobody ends up being
    # called "Unnamed Male" in the prose.
    if is_generic:
        if "name" in picks:
            instanced["name"] = picks["name"]
        else:
            instanced["name"] = new_id.replace("_", " ").title()

    # Description scrub: the template's `description` field is the
    # author's documentation of the template itself ("A neutral adult-
    # male template the narrator can materialise into when a user
    # directive introduces a named character not in the cast..."), not
    # a description of any specific character. Without this scrub, the
    # template meta-text ended up rendering as the materialised
    # character's description in the prompt — the model would then see
    # "[You — Kenji]\n A neutral adult-male template the narrator can
    # materialise into..." and lose all character grounding. Replace
    # with a minimal per-character one-liner derived from the random
    # picks so the prompt sees a real description. Narrator's explicit
    # `[set <id>.description = "..."]` overrides at render time via
    # deep-merge as usual.
    if is_generic:
        desc_parts: list[str] = []
        hair = picks.get("hair_color")
        eye = picks.get("eye_color")
        build = picks.get("build")
        skin = picks.get("skin_tone")
        body_word = "young woman" if "female" in (generic_template_id or "") else "young man"
        if hair or eye or build or skin:
            bits = []
            if build:
                bits.append(f"a {build} build")
            if hair:
                bits.append(f"{hair} hair")
            if eye:
                bits.append(f"{eye} eyes")
            if skin:
                bits.append(f"{skin} skin")
            instanced["description"] = (
                f"An ordinary {body_word} in the scene — " + ", ".join(bits) + "."
            )
        else:
            instanced["description"] = f"An ordinary {body_word} in the scene."

    # Tags scrub: the template carries `template` /
    # `narrator-materialisable` tags that document its purpose for
    # editors. Those are meta-tags, not character traits — drop them
    # from the materialised instance so the prompt's tag block doesn't
    # tell the model it's roleplaying "a template". Only for generics —
    # a real derive source's tags are genuine and kept.
    tags = instanced.get("tags") or []
    if is_generic and isinstance(tags, list):
        instanced["tags"] = [
            t for t in tags
            if t not in ("template", "narrator-materialisable", "reserved")
        ]

    ent.save_instance_entity(conversation_id, instanced)
    _pull_owned_outfits(conversation_id, generic_template_id)
    return True


def remove_from_conversation_cast(conversation_id: str, entity_id: str) -> bool:
    """Branch-scoped cast removal. Appends a ``cast_remove`` edit to
    the active leaf's ``applied_edits`` and drops the entity from the
    leaf's ``presence_snapshot`` so the cast widget hides them right
    away. Sibling branches stay untouched.

    Deliberately does NOT delete the instance file or mutate the
    shared instance scenario — those are part of the disk pool that
    every branch can pull from. Switching to a sibling branch where
    the entity was never removed will still see them.

    Returns True iff the leaf actually carried this entity (so the
    chat UI can flash the correct "removed N" toast). The user
    entity is never removable.
    """
    if entity_id == "user":
        return False
    template = ent.load_instance_entity(conversation_id, entity_id) or ent.get(entity_id) or {}
    etype = template.get("type")
    if etype not in ("character", "object"):
        return False

    conv = convs.load_conversation(conversation_id)
    if not conv:
        return False
    leaf_id = conv.get("active_path_leaf")
    leaf = conv["messages"].get(leaf_id) if leaf_id else None
    existed = False
    if leaf is not None:
        snap = dict(leaf.get("presence_snapshot") or {})
        presence = dict(snap.get("presence") or {})
        if entity_id in presence:
            existed = True
            presence.pop(entity_id, None)
            leaf["presence_snapshot"] = {**snap, "presence": presence}
        meta = leaf.setdefault("metadata", {})
        log = list(meta.get("applied_edits") or [])
        # Same last-edit dedupe as the add path: only a back-to-back
        # duplicate is a no-op; remove → add → remove must keep all three.
        if _last_cast_edit_kind(log, entity_id) != "cast_remove":
            log.append({"kind": "cast_remove", "ok": True, "id": entity_id})
            meta["applied_edits"] = log
        convs.save_conversation(conv)
    if etype == "character":
        _maybe_propagate_partner_after_cast_change(conversation_id)
    return existed


def _maybe_propagate_partner_after_cast_change(conversation_id: str) -> None:
    """Hook for setup_picker scenarios: every cast +/- of a character
    that's a member of the scenario's ``random_character_pool`` may
    change which partner the staging roots should be naming. This
    helper recomputes the active partner from the conversation cast
    and propagates it across every staging root's metadata so the
    [Scenario] block / opening prompt / sidebar all stay consistent
    regardless of which setup the user navigates to. No-op for
    scenarios without ``setup_picker``.
    """
    conv = convs.load_conversation(conversation_id)
    if not conv:
        return
    scenario_id = conv.get("scenario_id")
    if not scenario_id:
        return
    scenario = ent.load_instance_entity(conversation_id, scenario_id) or ent.get(scenario_id)
    if not scenario or not scenario.get("setup_picker"):
        return
    pool = scenario.get("random_character_pool") or []
    instance_ents = ent.load_instance_entities(conversation_id) or {}
    in_cast_pool = [
        eid for eid, e in instance_ents.items()
        if isinstance(e, dict) and e.get("type") == "character"
        and eid != "user" and eid in pool
    ]
    if not in_cast_pool:
        return
    # Prefer whoever's currently named on the active root, if they're
    # still in the cast pool; otherwise fall back to any cast pool
    # member.
    active_partner = None
    for m in conv.get("messages", {}).values():
        meta = m.get("metadata") or {}
        if meta.get("setup_active") and meta.get("staging"):
            active_partner = (meta.get("random_picks") or {}).get("partner")
            break
    partner_id = active_partner if active_partner in in_cast_pool else in_cast_pool[0]
    partner_name = (instance_ents.get(partner_id) or {}).get("name") or partner_id
    if convs.propagate_partner_to_staging_roots(conv, scenario, partner_id, partner_name):
        convs.save_conversation(conv)
