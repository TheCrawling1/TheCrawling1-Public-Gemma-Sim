"""Entity loading, validation, and scenario instancing.

Entity types: character, outfit, location, room, object, scenario.
Internal IDs are UUIDs so duplicate display names are fine.
"""
from __future__ import annotations

import copy
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from flask import current_app, g, has_request_context

from .storage import list_json_files, read_json, write_json

_LOAD_ALL_CACHE_KEY = "_entity_load_all_cache"


VALID_TYPES = {"character", "outfit", "clothing", "location", "room", "object", "scenario", "lore", "state", "pair_set"}

# Entity ids become filesystem path segments — reject anything that could
# traverse out of the data tree. Letters/digits/'.'/'_'/'-' only, and no
# ".." sequence. Every existing id already satisfies this.
_SAFE_ID_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9._-]+$")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def data_dir() -> Path:
    return Path(current_app.config["data_dir"])


def instances_dir() -> Path:
    return Path(current_app.config["instances_dir"])


def _type_dir(entity_type: str) -> Path:
    """Where loose (non-character-owned) entities of this type live."""
    if entity_type == "character":
        return data_dir() / "characters"
    if entity_type == "scenario":
        return data_dir() / "scenarios"
    if entity_type == "location":
        return data_dir() / "locations"
    if entity_type == "object":
        return data_dir() / "objects"
    if entity_type == "clothing":
        # Shared clothing pieces live here; per-character pieces live
        # under data/characters/<char_id>/clothing/<id>.json the same
        # way character-owned outfits do today.
        return data_dir() / "clothing"
    if entity_type in ("outfit", "room"):
        # outfits/rooms exist as files but typically owned by characters/locations.
        return data_dir() / f"{entity_type}s"
    if entity_type == "state":
        # Transient condition/affect overlays (drunk, exhausted, a belief).
        # See docs/clothing.md-analog design in ROADMAP "States as a
        # first-class primitive". Composes body_parts.base overlays +
        # an affect summary + a mannerism shift onto a character.
        return data_dir() / "states"
    if entity_type == "pair_set":
        # Global conditional dialogue-pair sets (Layer B of the dynamic
        # NPC pairs system): context-triggered {user,char} primer pairs
        # any focal can draw on (e.g. public_nudity_awkward). See
        # personas._collect_context_pairs.
        return data_dir() / "pairs"
    raise ValueError(f"Unknown entity type: {entity_type}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def new_id() -> str:
    return uuid.uuid4().hex


_CLOTHING_SLOT_INVENTORY = {
    # Sprite-aligned slots (8 — drive wardrobe.json layer composition).
    "top", "bottom", "bra", "panties", "pantyhose", "gloves", "legwear", "shoes",
    # Prose-only slots (5).
    "head", "face", "neck", "back", "overlay",
}


def validate_entity(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Entity must be a JSON object.")
    etype = data.get("type")
    if etype not in VALID_TYPES:
        raise ValueError(f"Invalid type {etype!r}.")
    if not data.get("id"):
        data["id"] = new_id()
    # The id is interpolated straight into filesystem paths by _file_for
    # (directory AND filename), so a client-supplied id must not be able
    # to traverse out of the data tree. Restrict to a safe charset; all
    # existing ids already match this.
    if not _SAFE_ID_RE.match(str(data["id"])):
        raise ValueError(f"Invalid id {data['id']!r} — ids may only contain letters, digits, '.', '_' and '-'.")
    data.setdefault("name", "")
    data.setdefault("description", "")
    data.setdefault("tags", [])
    data.setdefault("properties", {})
    data.setdefault("example_text", "")
    data.setdefault("children", [])
    if not isinstance(data["tags"], list):
        raise ValueError("tags must be a list.")
    if not isinstance(data["properties"], dict):
        raise ValueError("properties must be an object.")
    if not isinstance(data["children"], list):
        raise ValueError("children must be a list.")
    if etype == "clothing":
        _normalize_clothing_shape(data)
        _validate_clothing(data)
    if etype == "character":
        _validate_character(data)
    return data


def _normalize_clothing_shape(data: dict[str, Any]) -> None:
    """Coerce common near-miss clothing shapes into the canonical schema
    before validation.

    Narrator-generated `replace` edits (and hand authoring) routinely put
    the slot / states at the TOP level of the entity, or use the plural
    `slots: ["top"]` instead of `properties.slot: "top"`. Left alone those
    hard-fail in `_validate_clothing` ("properties.slot must be a non-empty
    string") and the piece never materializes — e.g. a narrator inventing a
    `glitter_crop_tee` mid-scene. Map the stray fields onto
    `properties.{slot,states,coverage}` so the piece is created instead.

    Only FILLS a canonical field when it's missing/empty; never overrides a
    value already in the right place. Scoped to clothing (called only for
    that type), so removing the stray top-level keys afterward is safe.
    """
    props = data.get("properties")
    if not isinstance(props, dict):
        return
    # slot — canonical is properties.slot (a non-empty string). Accept a
    # plural list and/or a top-level placement.
    if not (isinstance(props.get("slot"), str) and props.get("slot")):
        for src in (props.get("slots"), data.get("slot"), data.get("slots")):
            if isinstance(src, str) and src:
                props["slot"] = src
                break
            if isinstance(src, list) and src and isinstance(src[0], str) and src[0]:
                props["slot"] = src[0]
                break
    # states — canonical is properties.states (a non-empty list).
    if not (isinstance(props.get("states"), list) and props.get("states")):
        if isinstance(data.get("states"), list) and data["states"]:
            props["states"] = data["states"]
    # coverage — canonical is properties.coverage (a map). Accept top-level.
    if props.get("coverage") is None and isinstance(data.get("coverage"), dict):
        props["coverage"] = data["coverage"]
    # Drop the stray top-level / plural keys so they don't linger on the
    # saved entity or confuse downstream readers.
    for k in ("slot", "slots", "states", "coverage"):
        data.pop(k, None)
    props.pop("slots", None)


# Character property containers that are structured maps / lists. A
# JSON-tab typo that turns one into the wrong type would silently break
# rendering, so we type-check them at write time. `mannerisms` / `scent`
# / `body_hair` are deliberately NOT listed — `mannerisms` legitimately
# ships as either a string (some NPC cards) or a list, so strict typing
# would reject valid data.
_CHAR_DICT_FIELDS = ("personality", "body_parts", "worn")
_CHAR_LIST_FIELDS = ("dialogue_pairs", "conditional_pairs", "outfits", "inventory")


def _validate_character(data: dict[str, Any]) -> None:
    props = data.get("properties") or {}
    if not isinstance(props, dict):
        return
    for k in _CHAR_DICT_FIELDS:
        if k in props and not isinstance(props[k], dict):
            raise ValueError(f"character.properties.{k} must be an object")
    for k in _CHAR_LIST_FIELDS:
        if k in props and not isinstance(props[k], list):
            raise ValueError(f"character.properties.{k} must be a list")


def _validate_clothing(data: dict[str, Any]) -> None:
    """Schema check for v2 clothing pieces. See docs/clothing.md for the
    full spec. Catches the common authoring errors at write time:

      - Unknown slot (typo / outside the default inventory).
      - Missing or empty `states` list.
      - Coverage map keyed by a state name the piece didn't declare.
      - Per-part coverage entry that isn't a dict / lacks `covered` /
        lacks `description`.

    Per-character extension slots (e.g. `head_2`, `tail`) are allowed
    by passing through unknown slot names with a soft warning attached
    to the entity dict — strict rejection here would block authoring
    ettin / catgirl / whatever-future-shape characters. Authors who
    want strict validation can fork this gate.
    """
    props = data.get("properties") or {}
    slot = props.get("slot")
    if not isinstance(slot, str) or not slot:
        raise ValueError("clothing.properties.slot must be a non-empty string")
    if slot not in _CLOTHING_SLOT_INVENTORY:
        # Soft pass — per-character extension slots are valid. Stamp a
        # note on the entity so tooling can surface "non-default slot
        # used" without failing.
        data.setdefault("_warnings", []).append(
            f"slot {slot!r} is outside the default inventory "
            f"({sorted(_CLOTHING_SLOT_INVENTORY)})"
        )

    states = props.get("states")
    if not isinstance(states, list) or not states:
        raise ValueError("clothing.properties.states must be a non-empty list")
    if not all(isinstance(s, str) and s for s in states):
        raise ValueError("clothing.properties.states entries must be non-empty strings")

    coverage = props.get("coverage")
    if coverage is None:
        # An overlay piece that touches nothing still needs an empty
        # coverage map per state so the renderer's per-state lookup
        # is uniform. Fill in defaults.
        coverage = {s: {} for s in states}
        props["coverage"] = coverage
    if not isinstance(coverage, dict):
        raise ValueError("clothing.properties.coverage must be an object")

    declared_states = set(states)
    for state_name, parts in coverage.items():
        if state_name not in declared_states:
            raise ValueError(
                f"clothing.properties.coverage has entry for state "
                f"{state_name!r} which is not in states list {states!r}"
            )
        if not isinstance(parts, dict):
            raise ValueError(
                f"clothing.properties.coverage[{state_name!r}] must be an object"
            )
        for part_name, entry in parts.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"coverage[{state_name!r}][{part_name!r}] must be an object"
                )
            if "covered" not in entry or not isinstance(entry["covered"], bool):
                raise ValueError(
                    f"coverage[{state_name!r}][{part_name!r}] must have a "
                    f"`covered: bool` field"
                )
            if "description" in entry and not isinstance(entry["description"], str):
                raise ValueError(
                    f"coverage[{state_name!r}][{part_name!r}].description "
                    f"must be a string"
                )


# ---------------------------------------------------------------------------
# Loose template CRUD (the data/ tree, never modified by conversations)
# ---------------------------------------------------------------------------


# Process-level (cross-request) cache for load_all(): (token, parsed-map).
# The request-scoped flask.g cache collapses the dozens of load_all() calls in
# a single turn to one scan, but it dies at end of request, so every new turn
# re-read and re-parsed all ~1.1k JSON files from disk. This layer reuses the
# parsed map across requests, guarded by a cheap stat-only fingerprint of the
# data/ tree (`_tree_token`): no file is *read* unless the tree actually
# changed. A template write (save/delete) or any external edit — direct file
# edit, `git pull`, another worker process — bumps a file's mtime, the token
# changes, and the next load_all() rebuilds. The token reads shared filesystem
# state, so it invalidates correctly ACROSS processes (gunicorn workers), not
# just within one.
#
# Shared safely under the same read-only contract that already makes the
# request cache safe: callers treat load_all() results as templates and
# deepcopy before mutating (the "mutating an instance never touches templates"
# rule), and prompt assembly only reads. Never mutate a load_all() value in
# place — it now persists across requests, so an in-place edit would leak.
_PROCESS_CACHE: tuple[tuple, dict[str, dict[str, Any]]] | None = None


def _tree_token(files: list[Path]) -> tuple:
    """A cheap fingerprint of the data/ tree: one stat() per json file, no
    reads. Changes when any entity file is added, removed, modified, or
    resized. Mirrors load_all's input set (skips users.json) so a users.json
    write — which load_all ignores — doesn't force a needless rebuild."""
    parts: list[tuple] = []
    for f in files:
        if f.name == "users.json":
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        parts.append((f.as_posix(), st.st_mtime_ns, st.st_size))
    return tuple(parts)


def _invalidate_load_all_cache() -> None:
    """Drop both the request-scoped and process-scoped load_all caches (call
    after any template write). The process cache also self-invalidates via the
    data-tree token, but clearing it here makes a write take effect immediately
    in this process even on a filesystem whose mtime resolution might not have
    advanced between the read and the write."""
    global _PROCESS_CACHE
    _PROCESS_CACHE = None
    if has_request_context():
        g.pop(_LOAD_ALL_CACHE_KEY, None)


def load_all() -> dict[str, dict[str, Any]]:
    """Load every entity in data/ keyed by id.

    Two cache layers, both transparent (a miss always returns a fresh, correct
    map):

      - Request-scoped (flask.g): a single chat turn calls this dozens of times
        via get()/by_type(); the first call populates g, the rest reuse it with
        no stat and no read.
      - Process-scoped (`_PROCESS_CACHE`): reuses the parsed map across requests
        as long as the data/ tree is unchanged, verified by a stat-only token
        (`_tree_token`). On a hit, zero files are read. A write or external edit
        bumps an mtime, the token changes, and the map is rebuilt once.

    Outside a request context (scripts, the dump tool, background jobs) only the
    process layer applies — still always fresh, still no behavior change."""
    global _PROCESS_CACHE
    # Request-scoped fast path: no stat, no read.
    if has_request_context():
        cached = g.get(_LOAD_ALL_CACHE_KEY)
        if cached is not None:
            return cached

    # Process-scoped path: one directory walk + one stat per file to build the
    # token; reuse the parsed map when it matches.
    files = list_json_files(data_dir())
    token = _tree_token(files)
    pc = _PROCESS_CACHE
    if pc is not None and pc[0] == token:
        out = pc[1]
    else:
        out = {}
        for f in files:
            if f.name == "users.json":
                continue
            try:
                ent = read_json(f)
            except Exception:
                continue
            if isinstance(ent, dict) and ent.get("id") and ent.get("type") in VALID_TYPES:
                out[ent["id"]] = ent
        _PROCESS_CACHE = (token, out)

    if has_request_context():
        setattr(g, _LOAD_ALL_CACHE_KEY, out)
    return out


def by_type(entity_type: str) -> list[dict[str, Any]]:
    return [e for e in load_all().values() if e.get("type") == entity_type]


def get(entity_id: str) -> dict[str, Any] | None:
    return load_all().get(entity_id)


def _file_for(entity: dict[str, Any]) -> Path:
    """Pick a file path for a (template) entity.

    Layout:
      - character → data/characters/<id>/<id>.json
      - location  → data/locations/<id>/<id>.json
      - room      → data/locations/<owner>/rooms/<id>.json when an owning
                    location references it via children; else
                    data/locations/_orphan_rooms/<id>.json.
      - outfit    → keep in place if it already lives somewhere (characters
                    own most outfits in their folder); else data/outfits/.
      - object/scenario → data/<type>s/<id>.json.
    """
    etype = entity["type"]
    if etype == "character":
        folder = data_dir() / "characters" / entity["id"]
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{entity['id']}.json"
    if etype == "location":
        folder = data_dir() / "locations" / entity["id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "rooms").mkdir(exist_ok=True)
        return folder / f"{entity['id']}.json"
    if etype == "room":
        existing = _find_existing_path(entity["id"])
        if existing:
            return existing
        owner = _find_owning_location(entity["id"])
        if owner:
            folder = data_dir() / "locations" / owner / "rooms"
            folder.mkdir(parents=True, exist_ok=True)
            return folder / f"{entity['id']}.json"
        folder = data_dir() / "locations" / "_orphan_rooms"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{entity['id']}.json"
    if etype == "outfit":
        existing = _find_existing_path(entity["id"])
        if existing:
            return existing
        return _type_dir(etype) / f"{entity['id']}.json"
    if etype == "clothing":
        # Honour the existing-path location (character-owned pieces
        # live at data/characters/<owner>/clothing/<id>.json). Pieces
        # with an `owner` field but no existing file get routed
        # under that owner's character directory; otherwise drop into
        # the shared data/clothing/ pool.
        existing = _find_existing_path(entity["id"])
        if existing:
            return existing
        owner = (entity.get("properties") or {}).get("owner")
        if owner and (data_dir() / "characters" / owner).is_dir():
            folder = data_dir() / "characters" / owner / "clothing"
            folder.mkdir(parents=True, exist_ok=True)
            return folder / f"{entity['id']}.json"
        folder = _type_dir(etype)
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{entity['id']}.json"
    return _type_dir(etype) / f"{entity['id']}.json"


def _find_owning_location(room_id: str) -> str | None:
    """Return the id of the location whose children include this room id."""
    for ent in load_all().values():
        if ent.get("type") != "location":
            continue
        if room_id in (ent.get("children") or []):
            return ent.get("id")
    return None


def _find_existing_path(entity_id: str) -> Path | None:
    for f in list_json_files(data_dir()):
        if f.stem == entity_id:
            return f
        try:
            ent = read_json(f)
        except Exception:
            continue
        if isinstance(ent, dict) and ent.get("id") == entity_id:
            return f
    return None


def save(entity: dict[str, Any]) -> dict[str, Any]:
    entity = validate_entity(entity)
    write_json(_file_for(entity), entity)
    _invalidate_load_all_cache()
    return entity


def delete(entity_id: str) -> bool:
    path = _find_existing_path(entity_id)
    if not path:
        return False
    # A character owns its whole folder — `data/characters/<id>/` holds
    # the portrait, `images/`, and any owner-scoped clothing/outfit
    # files, not just `<id>.json`. Remove the folder so a deleted
    # character doesn't orphan its assets. The guard is exact
    # (`.../characters/<id>/<id>.json`) so non-character files (single
    # files, or shared dirs like `data/clothing/`) only unlink the file.
    parent = path.parent
    if parent.name == entity_id and parent.parent.name == "characters":
        import shutil
        shutil.rmtree(parent, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
    _invalidate_load_all_cache()
    return True


# ---------------------------------------------------------------------------
# Instancing
# ---------------------------------------------------------------------------


def instance_root(conversation_id: str) -> Path:
    return instances_dir() / conversation_id


def instance_entities_dir(conversation_id: str) -> Path:
    return instance_root(conversation_id) / "entities"


def create_instance_from_scenario(scenario_id: str, conversation_id: str) -> dict[str, Any]:
    """Deep-copy every entity referenced by the scenario into the instance dir.

    Returns a snapshot of the starting state computed from the scenario's
    `starting_state`. Mutating the instance never affects the templates.
    """
    all_entities = load_all()
    scenario = all_entities.get(scenario_id)
    if not scenario or scenario.get("type") != "scenario":
        raise ValueError(f"Scenario {scenario_id!r} not found.")

    referenced: set[str] = set()
    for key in ("characters", "locations", "objects", "outfits", "rooms", "lore"):
        for ref in scenario.get(key, []) or []:
            referenced.add(ref)
    # Random pools are NOT pre-instanced at creation — only the picks
    # rolled in `create_conversation_from_scenario` join `characters[]`
    # / `objects[]` and get instanced via the loop above. The other
    # pool members stay in the master library and only get instanced
    # when the user adds them via the cast +/- UI (which calls
    # `add_to_conversation_cast`). This keeps the conversation's cast
    # list and `[Items in scene]` prompt block tidy — they reflect
    # what's actually in scene, not the whole pool.

    # Auto-include children transitively so locations bring their rooms/objects.
    queue = list(referenced)
    while queue:
        eid = queue.pop()
        ent = all_entities.get(eid)
        if not ent:
            continue
        for child_id in ent.get("children", []) or []:
            if child_id not in referenced:
                referenced.add(child_id)
                queue.append(child_id)
        # A character may reference outfits via properties.outfits or current_outfit.
        if ent.get("type") == "character":
            for outfit_id in ent.get("properties", {}).get("outfits", []) or []:
                if outfit_id not in referenced:
                    referenced.add(outfit_id)
                    queue.append(outfit_id)
            current_outfit = ent.get("properties", {}).get("current_outfit")
            if current_outfit and current_outfit not in referenced:
                referenced.add(current_outfit)
                queue.append(current_outfit)
        # An outfit may extend a base outfit; pull the chain into the instance
        # so the resolver always finds the parent locally.
        if ent.get("type") == "outfit":
            base_id = (ent.get("properties") or {}).get("extends")
            if isinstance(base_id, str) and base_id and base_id not in referenced:
                referenced.add(base_id)
                queue.append(base_id)

    inst_dir = instance_entities_dir(conversation_id)
    inst_dir.mkdir(parents=True, exist_ok=True)

    instanced: dict[str, dict[str, Any]] = {}
    for eid in referenced:
        ent = all_entities.get(eid)
        if not ent:
            continue
        copied = copy.deepcopy(ent)
        copied["_template_id"] = eid  # Provenance only; mutations stay instanced.
        write_json(inst_dir / f"{eid}.json", copied)
        instanced[eid] = copied

    # Pull every outfit owned by an instanced character into the instance,
    # even when the character entity doesn't list it under
    # `properties.outfits`. Without this the narrator only sees outfits a
    # human author happened to register on the character card; new outfits
    # added to the data dir (e.g. `dex_shirt_up`) would be
    # invisible to the cast summary and to `[outfit char -> ...]` directive
    # resolution. Scoped to characters already in the instance so we don't
    # drag in unrelated cast members' wardrobes.
    instanced_char_ids = {
        eid for eid, e in instanced.items()
        if e.get("type") == "character" and eid != "user"
    }
    for tmpl_id, tmpl in all_entities.items():
        if tmpl.get("type") != "outfit":
            continue
        owner = (tmpl.get("properties") or {}).get("owner")
        if owner not in instanced_char_ids or tmpl_id in instanced:
            continue
        copied = copy.deepcopy(tmpl)
        copied["_template_id"] = tmpl_id
        write_json(inst_dir / f"{tmpl_id}.json", copied)
        instanced[tmpl_id] = copied
        # Walk extends so any base layer the outfit needs comes along too.
        base_id = (tmpl.get("properties") or {}).get("extends")
        while isinstance(base_id, str) and base_id and base_id not in instanced:
            base = all_entities.get(base_id)
            if not base or base.get("type") != "outfit":
                break
            base_copy = copy.deepcopy(base)
            base_copy["_template_id"] = base_id
            write_json(inst_dir / f"{base_id}.json", base_copy)
            instanced[base_id] = base_copy
            base_id = (base.get("properties") or {}).get("extends")

    # Also stash a copy of the scenario itself.
    scenario_copy = copy.deepcopy(scenario)
    scenario_copy["_template_id"] = scenario_id
    write_json(inst_dir / f"{scenario_id}.json", scenario_copy)
    instanced[scenario_id] = scenario_copy

    # Scenario-defined custom outfits: full outfit entities that exist only
    # for this scenario instance. Lets a 'beach' scenario carry its own
    # bikini without polluting the character's canonical outfit list.
    # We also follow each custom outfit's `extends` chain so the base
    # outfit (e.g. thin_shirt_generic) gets instanced too — without it
    # _resolved_outfit can't merge coverage from the base layer at
    # prompt-assembly time.
    custom_outfits = scenario.get("custom_outfits") or []
    if isinstance(custom_outfits, list):
        for outfit_data in custom_outfits:
            if not isinstance(outfit_data, dict):
                continue
            try:
                outfit = validate_entity({**outfit_data})
            except Exception:
                continue
            if outfit.get("type") != "outfit":
                continue
            outfit["_scenario_custom"] = scenario_id
            write_json(inst_dir / f"{outfit['id']}.json", outfit)
            instanced[outfit["id"]] = outfit
            # Walk extends chain for the custom outfit and pull each
            # ancestor template into the instance dir.
            base_id = (outfit.get("properties") or {}).get("extends")
            visited: set[str] = set()
            while isinstance(base_id, str) and base_id and base_id not in visited:
                visited.add(base_id)
                if base_id in instanced:
                    base_id = (instanced[base_id].get("properties") or {}).get("extends")
                    continue
                base_template = all_entities.get(base_id)
                if not base_template or base_template.get("type") != "outfit":
                    break
                copied = copy.deepcopy(base_template)
                copied["_template_id"] = base_id
                write_json(inst_dir / f"{base_id}.json", copied)
                instanced[base_id] = copied
                base_id = (base_template.get("properties") or {}).get("extends")

    # Scenario-level character overrides: per-character partial entity that
    # gets deep-merged into the instanced character. Uses the shared deep
    # merge so e.g. character_overrides.iris.properties.body_parts.head
    # patches just the head without clobbering chest/arms/etc.
    from .merge import deep_merge
    overrides = scenario.get("character_overrides") or {}
    if isinstance(overrides, dict):
        for char_id, patch in overrides.items():
            if char_id not in instanced or not isinstance(patch, dict):
                continue
            base = instanced[char_id]
            deep_merge(base, patch)
            write_json(inst_dir / f"{char_id}.json", base)
            instanced[char_id] = base

    starting_state = _resolve_starting_state(scenario, instanced)

    # Always seed a `user` instance entity so the narrator-edit grammar
    # ([move user -> X], [outfit user -> Y], [set user.body_parts.head.base = ...])
    # can target a real entity. Picking a user-tagged persona card later
    # via /api/conversations/<cid>/user-persona overwrites this stub
    # with a deep-copy of the chosen template.
    user_path = inst_dir / "user.json"
    if not user_path.exists():
        user_entity = {
            "id": "user",
            "type": "character",
            "name": "User",
            "description": "",
            "tags": ["user"],
            "children": [],
            "properties": {},
            "example_text": "",
        }
        write_json(user_path, user_entity)
        instanced["user"] = user_entity

    return {
        "scenario_id": scenario_id,
        "entities": instanced,
        "starting_state": starting_state,
        "created_at": int(time.time()),
    }


def _resolve_starting_state(
    scenario: dict[str, Any],
    instanced: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build the initial presence snapshot from scenario.starting_state."""
    raw = scenario.get("starting_state", {}) or {}
    presence: dict[str, dict[str, Any]] = {}
    for char_id in scenario.get("characters", []) or []:
        defaults = raw.get(char_id, {}) or {}
        presence[char_id] = {
            "location": defaults.get("location"),
            "room": defaults.get("room"),
            "outfit": defaults.get("outfit")
            or instanced.get(char_id, {}).get("properties", {}).get("current_outfit"),
        }
    return {
        "presence": presence,
        "objects_present": dict(raw.get("objects_present", {}) or {}),
    }


def load_instance_entity(conversation_id: str, entity_id: str) -> dict[str, Any] | None:
    return read_json(instance_entities_dir(conversation_id) / f"{entity_id}.json")


def save_instance_entity(conversation_id: str, entity: dict[str, Any]) -> dict[str, Any]:
    entity = validate_entity(entity)
    write_json(instance_entities_dir(conversation_id) / f"{entity['id']}.json", entity)
    return entity


def load_instance_entities(conversation_id: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in list_json_files(instance_entities_dir(conversation_id)):
        ent = read_json(f)
        if isinstance(ent, dict) and ent.get("id"):
            out[ent["id"]] = ent
    return out


def apply_patch(conversation_id: str, entity_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge `patch` into the instanced entity. Lists/dicts in patch replace.

    A more sophisticated JSON Patch (RFC 6902) implementation can land later;
    shallow merge handles the common Narrator cases for now.
    """
    ent = load_instance_entity(conversation_id, entity_id)
    if not ent:
        raise ValueError(f"Instance entity {entity_id!r} not found.")
    for k, v in patch.items():
        if k == "id":
            continue
        if isinstance(v, dict) and isinstance(ent.get(k), dict):
            ent[k] = {**ent[k], **v}
        else:
            ent[k] = v
    return save_instance_entity(conversation_id, ent)


def unset_paths(conversation_id: str, entity_id: str,
                paths: list[list[str]]) -> dict[str, Any]:
    """Drop each dot-path subtree from the disk instance entity, then persist.

    Used by the wholesale-replace patch path when a conversation has no active
    leaf (scenario setup) — the subtree is removed so a following shallow patch
    can't preserve stale keys under it. Missing paths are no-ops."""
    ent = load_instance_entity(conversation_id, entity_id)
    if not ent:
        raise ValueError(f"Instance entity {entity_id!r} not found.")
    for path in paths or []:
        if not (isinstance(path, list) and path and all(isinstance(p, str) for p in path)):
            continue
        cur: Any = ent
        for key in path[:-1]:
            if not isinstance(cur, dict) or key not in cur:
                cur = None
                break
            cur = cur[key]
        if isinstance(cur, dict):
            cur.pop(path[-1], None)
    return save_instance_entity(conversation_id, ent)


def replace_entity(conversation_id: str, entity: dict[str, Any]) -> dict[str, Any]:
    return save_instance_entity(conversation_id, entity)


def referenced_in_scenario(scenario: dict[str, Any]) -> Iterable[str]:
    for key in ("characters", "locations", "objects", "outfits", "rooms"):
        for ref in scenario.get(key, []) or []:
            yield ref
