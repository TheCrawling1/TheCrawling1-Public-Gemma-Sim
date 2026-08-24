"""Record narrator-extracted edits on the message that produced them.

Edits used to be applied to the conversation's instance entity files
directly. That broke branching: an edit made on branch A would persist
to the entity file and leak into branch B when the user navigated back.

The new model is path-based: edits are written into the message's
`metadata.applied_edits` and never touched onto the instance file.
Rendering (`app/effective.py`) walks root → leaf and replays the
collected edits onto a deep-copy of the baseline instance entity, so
each branch sees exactly the changes that happened along its own path.

What still touches files:
  * `[outfit]` brings the outfit template into the instance dir if it
    isn't already (so the outfit's coverage data is loadable). The
    character file itself is NOT mutated; `current_outfit` lives on
    the message's presence_snapshot via the returned presence_patch.

The user persona is a real instance entity at id `user` (seeded by
`create_instance_from_scenario`, populated by the user-persona route
when the user picks a card). `[set user.X = Y]` / `[outfit user -> Y]`
/ `[move user -> Y]` therefore route through the same path as any
other character — no special-casing needed here.
"""
from __future__ import annotations

import copy
import time
from typing import Any

from . import entities as ent
from .merge import UNSET_MARKER as _UNSET_MARKER, deep_merge as _deep_merge, slice_for_deep_patch as _slice_for_deep_patch


def apply_edits(
    conversation_id: str,
    edits: list[dict[str, Any]],
    parent_snapshot: dict[str, Any] | None,
    user_persona: dict[str, Any] | None = None,  # legacy parameter, unused
    *,
    existing_cast_chars: set[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Return (presence_patch_by_char, applied_log).

    `presence_patch_by_char` is the per-character delta the caller
    merges into the new message's `presence_snapshot` (move/outfit
    only). `applied_log` is the list of normalized edit entries the
    caller stores on the message under `metadata.applied_edits`; the
    effective-state computer replays them at render time.

    `parent_snapshot` is currently used only to record before-images
    on move entries (so the UI can show "moved from X to Y"). It is
    no longer consulted for entity-state computation since state is
    rebuilt from the path on demand.

    `user_persona` is retained as a no-op parameter for backwards
    compatibility with callers that still pass it; user edits are
    now regular entity edits against the `user` instance entity.

    `existing_cast_chars` is the set of character ids already on the
    parent path's effective cast. When provided, paired `cast_add`
    entries for those characters are dropped from the output log —
    they'd be no-ops at replay time (`effective_cast_at` uses set
    semantics) but they pollute the applied-edits panel where, e.g.,
    an auto-state wardrobe patch on `iris` would otherwise log a
    redundant `cast_add: iris`. New cast_adds emitted in-batch
    augment the set so a second patch on the same char in the same
    batch doesn't re-emit either. Defaults to None (legacy behavior:
    every character-target edit gets a paired cast_add). Callers
    typically pass `effective_cast_at(conv, parent_id)["characters"]`.
    """
    del user_persona  # see docstring
    presence_patch: dict[str, dict[str, Any]] = {}
    log: list[dict[str, Any]] = []
    parent_presence = (parent_snapshot or {}).get("presence", {}) if parent_snapshot else {}
    # Track which characters already exist on the parent path's cast
    # OR were just cast_added earlier in this batch — so we can drop
    # redundant pair-cast_adds without losing the genuinely new ones.
    # When the caller doesn't pass a set (legacy / unknown context),
    # leave it None and emit every cast_add (back-compat).
    in_cast: set[str] | None = (
        set(existing_cast_chars) if existing_cast_chars is not None else None
    )

    # Pre-scan: collect any `_materialize_from` hints across the
    # whole edit batch up front. The narrator may emit the hint in a
    # different patch from the one that first triggers materialize
    # (e.g. `[set new_id.name = "X"]` arrives before
    # `[set new_id.properties._materialize_from = "generic_nude_male"]`).
    # Pre-scanning means the materialize call uses the correct
    # template regardless of which patch comes first in the edit list.
    template_hints: dict[str, str] = {}
    _ALLOWED_TEMPLATES = (
        "generic_male", "generic_nude_male",
        "generic_female", "generic_nude_female",
    )
    # derive/full modes let a `_materialize_from` hint name any existing
    # character (not just a generic) so a new id can be cloned from it.
    _derive_ok = _creation_mode_for(conversation_id) in ("derive", "full")
    for e in edits or []:
        if not isinstance(e, dict) or e.get("kind") != "patch":
            continue
        eid = e.get("id")
        if not eid or eid == "user":
            continue
        data = e.get("data") or {}
        if not isinstance(data, dict):
            continue
        hint = (data.get("properties") or {}).get("_materialize_from")
        if not isinstance(hint, str) or not hint:
            continue
        if hint in _ALLOWED_TEMPLATES:
            template_hints[eid] = hint
        elif (
            _derive_ok
            and hint != eid
            and _is_character_target(conversation_id, hint)
        ):
            # Derive-from-existing: clone the named source character.
            template_hints[eid] = hint

    # Name → id map for alias resolution: lets the narrator address
    # characters by either id (`guy_1`) or current name (`Kenji`) in
    # directives. Seeded from instance entities + global templates;
    # updated as in-batch `[set <id>.name = "..."]` patches land so
    # later edits in the same batch see fresh renames.
    name_to_id = _build_name_to_id_map(conversation_id)

    for edit in edits:
        # Alias-resolve the edit's target id BEFORE recording. If the
        # narrator emitted `[set Kenji.notes.servant = "iris"]` after
        # an earlier `[set guy_1.name = "Kenji"]`, the resolver maps
        # "Kenji" → guy_1 so the patch lands on the right entity
        # instead of materializing a phantom one. Exact-id match
        # always wins; only unknown ids check the name map.
        edit = _aliased_edit(conversation_id, edit, name_to_id)
        # Reroute property-only keys the model wrote at the entity top
        # level (a common `[set <id>.body_parts.head.base = ...]` slip that
        # drops `.properties.`) back under `properties`, so the overlay
        # actually lands where the renderer reads it instead of as a junk
        # top-level field.
        edit = _normalize_misplaced_properties(edit)
        try:
            entry = _record_edit(
                conversation_id, edit, parent_presence, presence_patch,
                template_hints=template_hints,
            )
        except Exception as e:
            entry = {"kind": edit.get("kind"), "ok": False, "error": str(e), "edit": edit}
        if entry is None:
            continue
        entries = entry if isinstance(entry, list) else [entry]
        # Update name_to_id as set-name patches land so later edits
        # in the batch can alias against the just-assigned name.
        for e in entries:
            if (
                e
                and e.get("ok")
                and e.get("kind") == "patch"
                and isinstance(e.get("data"), dict)
                and isinstance(e["data"].get("name"), str)
            ):
                name_to_id[_normalize_name(e["data"]["name"])] = e["id"]
        # Filter redundant cast_adds against the running set. Keeps
        # the first cast_add for any genuinely new char (the
        # sibling-branch fix from 1f3ee21 still works) and drops
        # subsequent ones plus all cast_adds for baseline / already-
        # on-path characters. cast_remove invalidates membership so a
        # later cast_add can re-fire correctly.
        for e in entries:
            if not e:
                continue
            kind = e.get("kind")
            eid = e.get("id")
            if in_cast is not None and kind == "cast_add" and eid:
                if eid in in_cast:
                    continue
                in_cast.add(eid)
            elif in_cast is not None and kind == "cast_remove" and eid:
                in_cast.discard(eid)
            log.append(e)
    # Stamp when each edit was made so the Applied-edits timeline can show
    # a real time (not the source message's created_at). setdefault keeps
    # any caller-supplied made_at.
    _now = int(time.time())
    for _e in log:
        if isinstance(_e, dict):
            _e.setdefault("made_at", _now)
    return presence_patch, log


def revert(conversation_id: str, applied: dict[str, Any]) -> dict[str, Any]:
    """Legacy entry point. With path-based effective state, reverting
    an edit is just deleting the message that recorded it (the active
    path no longer includes it, so the replay drops it). This stub is
    retained for any caller that still invokes it; it always returns
    ok so the route layer can short-circuit cleanly."""
    return {"ok": True, "note": "path-based replay: delete the message to revert"}


# ---------------------------------------------------------------------------
# Internals


def _is_character_target(cid: str, eid: str) -> bool:
    """Return True iff `eid` resolves to a character entity (instance
    or global template) and isn't the user. Used by the edit handlers
    to gate paired-cast_add emission — only character entities need
    cast membership tracked per branch."""
    if not eid or eid == "user":
        return False
    inst = ent.load_instance_entity(cid, eid)
    if isinstance(inst, dict) and inst.get("type") == "character":
        return True
    tmpl = ent.get(eid)
    return isinstance(tmpl, dict) and tmpl.get("type") == "character"


_VALID_MATERIALIZE_TEMPLATES = (
    "generic_male", "generic_nude_male",
    "generic_female", "generic_nude_female",
)

# Per-conversation narrator character-creation modes
# (``conv.settings.narrator_controls.character_creation_mode``):
#   off     — legacy: new ids may only skin from the four generics above.
#   derive  — a ``_materialize_from`` hint may name ANY existing character
#             (instance or library template); the new id is cloned from it,
#             so "a doll version of Serena" keeps her face/hair/frame and
#             the narrator overlays only what changed.
#   custom  — the narrator may author a full new character via a fenced
#             ```edits``` replace block (handled by the existing replace
#             path + a prompt worked example); no apply-side change needed.
#   full    — derive AND custom.
_CREATION_MODES = ("off", "derive", "custom", "full")


def _creation_mode_for(cid: str) -> str:
    """Read ``narrator_controls.character_creation_mode`` for a conversation.
    Returns ``"off"`` when unset/invalid. ``derive``/``full`` permit the
    derive-from-existing-character path below."""
    try:
        from . import conversations as _convs
        conv = _convs.load_conversation(cid) or {}
    except Exception:
        return "off"
    m = ((conv.get("settings") or {}).get("narrator_controls") or {}).get(
        "character_creation_mode"
    )
    return m if m in _CREATION_MODES else "off"


def _build_name_to_id_map(cid: str) -> dict[str, str]:
    """Return ``{normalized_name: entity_id}`` for every character the
    narrator can plausibly address by name.

    Sources, in resolution priority (later wins on key collision):
      1. Global character templates — so directives can address e.g.
         ``Iris`` before she's been instanced into the conversation.
      2. Conversation instance characters — these win over templates
         because a per-conversation rename (``[set guy_1.name = "Kenji"]``
         landed on a prior turn) shouldn't be shadowed by a global
         "Kenji" template if one happens to exist.

    Names are normalized to lower-case, stripped, single-spaced. The
    in-batch update path lives in ``apply_edits``: as ``[set <id>.name =
    "X"]`` patches land, the caller appends to the map so later edits
    in the same batch can alias against the just-assigned name.

    Used by ``_resolve_alias``; without it the narrator's
    ``[set kenji.notes.X = Y]`` after an earlier
    ``[set guy_1.name = "Kenji"]`` would materialize a brand-new
    generic entity with id="kenji" instead of routing the patch to
    guy_1 (the entity whose effective name is now "Kenji").
    """
    out: dict[str, str] = {}
    try:
        for c in ent.by_type("character"):
            cid_ = c.get("id")
            name = c.get("name")
            if isinstance(cid_, str) and isinstance(name, str) and name.strip():
                out[_normalize_name(name)] = cid_
    except Exception:
        pass
    # Instance characters win over templates (the per-conversation
    # rename should take precedence).
    try:
        for cid_, inst in (ent.load_instance_entities(cid) or {}).items():
            if not isinstance(inst, dict) or inst.get("type") != "character":
                continue
            name = inst.get("name")
            if isinstance(name, str) and name.strip():
                out[_normalize_name(name)] = cid_
    except Exception:
        pass
    return out


def _normalize_name(s: str) -> str:
    """Lower-case + collapse whitespace, for case-insensitive name match."""
    if not isinstance(s, str):
        return ""
    return " ".join(s.strip().split()).lower()


def _resolve_alias(
    cid: str,
    ident: str,
    name_to_id: dict[str, str],
) -> str:
    """Resolve ``ident`` to an entity id.

    Exact-id match wins: if `ident` matches an existing instance entity
    or a global template id, return it unchanged. Otherwise look it up
    in `name_to_id` (lower-cased) and return the mapped id when found.
    On miss, return `ident` unchanged so the caller can fall through
    to materialize.

    The behaviour gives the narrator-add prompt the flexibility to use
    either ids or display names in directives — they round-trip
    identically.
    """
    if not ident or ident == "user":
        return ident
    # Exact id match wins.
    if ent.load_instance_entity(cid, ident) is not None or ent.get(ident) is not None:
        return ident
    resolved = name_to_id.get(_normalize_name(ident))
    if resolved:
        return resolved
    return ident


def _aliased_edit(cid: str, edit: dict[str, Any], name_to_id: dict[str, str]) -> dict[str, Any]:
    """Return a copy of `edit` with its target id field resolved via
    `_resolve_alias`. The id field name depends on the edit kind."""
    if not isinstance(edit, dict):
        return edit
    kind = edit.get("kind")
    key = "character_id" if kind in ("move", "outfit") else "id"
    raw = edit.get(key)
    if not isinstance(raw, str) or not raw:
        return edit
    resolved = _resolve_alias(cid, raw, name_to_id)
    if resolved == raw:
        return edit
    return {**edit, key: resolved}


def _ensure_character_present(
    cid: str,
    eid: str,
    template_hints: dict[str, str] | None,
) -> bool:
    """Ensure ``eid`` resolves to a character instance in this conversation.

    Tries (in order):
      1. ``layers.auto_instance_character`` — pulls a real character
         template into the instance dir.
      2. ``layers.materialize_from_generic`` — mints a brand-new id
         from a generic template. Uses the narrator-supplied
         ``_materialize_from`` hint when present, the per-conversation
         ``narrator_controls.default_materialize_template`` setting
         when not, and ``generic_male`` as the final fallback.

    Returns True iff a new instance was created on this call. Returns
    False both when the entity was already present and when neither
    fallback could resolve the id.

    Used by every ``_record_edit`` path that mutates an entity by id —
    ``patch``, ``move``, ``outfit``, ``unset``. Without this, only
    ``patch`` had the full pattern and the other three kinds silently
    failed on brand-new ids (the narrator emits
    ``[move jake -> bedroom]`` for a never-before-seen ``jake`` and
    the move record lands but no entity exists; same for
    ``[outfit jake -> X]`` and ``[unset jake.X]``).
    """
    if eid == "user":
        return False
    if ent.load_instance_entity(cid, eid) is not None:
        return False
    from . import layers
    was_new = layers.auto_instance_character(cid, eid)
    if was_new:
        return True
    if ent.get(eid) is not None:
        # Template exists but auto_instance refused for some other
        # reason (e.g. it's a user-locked persona template). Leave as-is.
        return False
    hint = (template_hints or {}).get(eid)
    if not hint:
        try:
            from . import conversations as _convs
            _conv = _convs.load_conversation(cid) or {}
            controls = (_conv.get("settings") or {}).get("narrator_controls") or {}
            default_tmpl = controls.get("default_materialize_template")
            mode = controls.get("character_creation_mode")
            mode = mode if mode in _CREATION_MODES else "off"
            if isinstance(default_tmpl, str) and default_tmpl:
                if default_tmpl in _VALID_MATERIALIZE_TEMPLATES:
                    hint = default_tmpl
                elif mode in ("derive", "full") and _is_character_target(cid, default_tmpl):
                    hint = default_tmpl
        except Exception:
            hint = None
    if hint:
        return layers.materialize_from_generic(cid, eid, generic_template_id=hint)
    return layers.materialize_from_generic(cid, eid)
# ---------------------------------------------------------------------------


# Keys that live ONLY under `properties` in the entity schema — never at
# the entity top level. When the narrator emits a patch that puts one of
# these at the top level (dropping the `.properties.` segment from the
# path), reroute it under `properties` so body/appearance/state overlays
# actually apply. Deliberately excludes real top-level fields (id, type,
# name, description, tags, example_text, children) and the narrator-state
# keys that _extra_state_lines intentionally allows at the top level.
_PROPERTY_ONLY_KEYS = frozenset({
    "body_parts", "personality", "mannerisms", "worn", "current_outfit",
    "outfits", "outfit_overrides", "goals", "stats", "relationships",
    "notes", "scent", "body_hair", "coverage", "clothing_slots", "garments",
    "inventory", "signature_physical_tells", "first_message",
})


def _deep_merge_into(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``src`` into ``dst`` (dicts merge, else overwrite)."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge_into(dst[k], v)
        else:
            dst[k] = v
    return dst


def _normalize_misplaced_properties(edit: dict[str, Any]) -> dict[str, Any]:
    """Move any ``_PROPERTY_ONLY_KEYS`` a patch put at the entity top level
    under ``data['properties']``. No-op for non-patch edits or clean data.

    Fixes the common model slip of writing ``[set <id>.body_parts.head.base
    = ...]`` (which parses to ``data={'body_parts': {...}}``) instead of
    ``[set <id>.properties.body_parts.head.base = ...]`` — without this the
    overlay lands as a junk top-level field and the real
    ``properties.body_parts`` is never touched, so e.g. a hair recolor
    silently fails to render."""
    if not isinstance(edit, dict) or edit.get("kind") != "patch":
        return edit
    data = edit.get("data")
    if not isinstance(data, dict):
        return edit
    misplaced = [k for k in data if k in _PROPERTY_ONLY_KEYS]
    if not misplaced:
        return edit
    edit = dict(edit)
    data = dict(data)
    props = dict(data.get("properties") or {})
    for k in misplaced:
        val = data.pop(k)
        if isinstance(props.get(k), dict) and isinstance(val, dict):
            _deep_merge_into(props[k], val)
        else:
            props[k] = val
    data["properties"] = props
    edit["data"] = data
    return edit


def _record_edit(
    cid: str,
    edit: dict[str, Any],
    parent_presence: dict[str, dict[str, Any]],
    presence_patch: dict[str, dict[str, Any]],
    *,
    template_hints: dict[str, str] | None = None,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Normalize one edit into an applied-log entry. Does not touch
    the instance entity file (path-based replay rebuilds state at
    render time). Outfit edits still pull the outfit template into
    the instance dir so rendering can find it; that's pure file
    management, not state mutation.

    Returns None to skip, a single entry, or a list of entries. The
    list form is used when an auto-instance side-effect needs to emit
    a synthetic ``cast_add`` entry alongside the original edit so the
    branch's effective cast picks up the freshly-materialized char.
    """
    kind = edit.get("kind")

    if kind == "cast_add":
        eid = edit.get("id")
        if not eid:
            return None
        from . import layers
        layers.instance_entity_into_conversation(cid, eid)
        return {"kind": "cast_add", "ok": True, "id": eid}

    if kind == "cast_remove":
        eid = edit.get("id")
        if not eid or eid == "user":
            return None
        return {"kind": "cast_remove", "ok": True, "id": eid}

    if kind == "patch":
        eid = edit.get("id")
        data = edit.get("data") or {}
        if not eid or not isinstance(data, dict):
            return None
        # Auto-instance off-cast characters + materialize brand-new ids
        # from a generic template. See `_ensure_character_present` for
        # the full pattern. Without this, the patch is recorded ok=True
        # (template exists) but `effective._replay_edit` silently drops
        # it at render time because there's no instance baseline.
        was_new = _ensure_character_present(cid, eid, template_hints)
        # Reject if no such entity is around — match the previous
        # behavior so authors get a clear "missing entity" error.
        if ent.load_instance_entity(cid, eid) is None and ent.get(eid) is None:
            return {"kind": "patch", "ok": False, "error": f"{eid} not found", "edit": edit}
        entry = {"kind": "patch", "ok": True, "id": eid, "data": data}
        # Emit a paired cast_add for character entities so the entity
        # joins THIS branch's cast. The instance file is shared across
        # branches but cast membership is branch-scoped: if a sibling
        # branch already materialized the same id, the instance exists
        # on disk but this branch's path has no cast_add for it, so
        # `effective_cast_at` would (correctly) exclude it from this
        # branch's cast and `branch_filter` would hide it from the
        # prompt. Emit cast_add every time we patch a character entity;
        # path-replay uses set semantics so a duplicate cast_add for a
        # baseline character (e.g. iris in the_marginalia) is a no-op.
        if _is_character_target(cid, eid):
            return [{"kind": "cast_add", "ok": True, "id": eid}, entry]
        return entry

    if kind == "replace":
        eid = edit.get("id")
        data = edit.get("data") or {}
        if not eid or not isinstance(data, dict):
            return None
        if eid == "user":
            return {"kind": "replace", "ok": False,
                    "error": "cannot replace the user entity", "edit": edit}

        # Normalize: the directive's target id wins over any
        # inconsistent `id` field in the payload.
        data = dict(data)
        data["id"] = eid

        # Materialize if this id is new (no template, no instance).
        # Without this, `effective._replay_edit` silently drops the
        # edit at line 54 ("Edit references something not in the
        # instance dir; skip") because `load_instance_entities`
        # finds no baseline to apply the replace onto.
        #
        # Mirrors the off-cast character auto-instance idiom in the
        # patch handler above — file-management, not state-mutation.
        # Scoped to brand-new ids so it can't overwrite an existing
        # instance entity (the replace overlay still applies via
        # _replay_edit when a baseline already exists).
        existing_inst = ent.load_instance_entity(cid, eid)
        existing_tmpl = ent.get(eid)
        is_new_entity = existing_inst is None and existing_tmpl is None
        cast_kind: str | None = None
        if is_new_entity:
            if data.get("type") not in ent.VALID_TYPES:
                return {"kind": "replace", "ok": False,
                        "error": (f"replace target {eid!r} is new; data must "
                                  f"include a valid `type` field (one of "
                                  f"{sorted(ent.VALID_TYPES)})"),
                        "edit": edit}
            try:
                ent.save_instance_entity(cid, data)
            except Exception as e:
                return {"kind": "replace", "ok": False,
                        "error": f"materialize failed: {e}", "edit": edit}
            # Pair with a cast_add so `effective_cast_at` picks the
            # new entity up on path replay. Widened beyond character /
            # object: rooms + locations need cast_add for branch
            # isolation (narrator mints `town_diner` on branch A;
            # sibling branch B should not see it). Outfits get
            # cast_add too for symmetry with the future clothing-add
            # flow — branch_filter doesn't gate outfits today, so the
            # entry is currently inert for outfits, but landing it
            # now means a later branch_filter widening doesn't need
            # to backfill historic conversations.
            etype = data.get("type")
            if etype in ("object", "character", "room", "location", "outfit"):
                cast_kind = etype

        entry = {"kind": "replace", "ok": True, "id": eid, "data": data}
        if cast_kind:
            return [{"kind": "cast_add", "ok": True, "id": eid}, entry]
        return entry

    if kind == "next":
        # Turn-handoff directive. Records who the just-completed
        # speaker is handing the turn to. Read by the auto turn-mode
        # branch in stream._compute_next_responder after the message
        # is persisted. No entity mutation; this is metadata only.
        nid = edit.get("id")
        if not nid:
            return None
        return {"kind": "next", "ok": True, "id": nid}

    if kind == "unset":
        eid = edit.get("id")
        path = edit.get("path") or []
        if not eid or not path:
            return None
        # Same auto-instance / materialize-from-generic fallback as the
        # patch path — without it, an `[unset jake.X]` against a
        # brand-new id records ok=True but `effective._replay_edit`
        # silently drops the edit at render time because jake isn't
        # in the instance map.
        was_new = _ensure_character_present(cid, eid, template_hints)
        if ent.load_instance_entity(cid, eid) is None and ent.get(eid) is None:
            return {"kind": "unset", "ok": False, "error": f"{eid} not found", "edit": edit}
        entry = {"kind": "unset", "ok": True, "id": eid, "path": path}
        # Branch-cast-membership companion — see patch handler for the
        # full rationale on why we emit cast_add every time and not
        # just when was_new fires.
        if _is_character_target(cid, eid):
            return [{"kind": "cast_add", "ok": True, "id": eid}, entry]
        return entry

    if kind == "outfit":
        char_id = edit.get("character_id")
        outfit_id = edit.get("outfit_id")
        if not char_id or not outfit_id:
            return None
        # Same auto-instance / materialize-from-generic fallback as the
        # patch path — without it, an `[outfit jake -> X]` directive
        # before any prior `[set jake.X = ...]` against a brand-new id
        # rejects with "not a character" because jake hasn't been
        # materialized yet.
        was_new_char = _ensure_character_present(cid, char_id, template_hints)
        # Verify the character exists (instance or template).
        char = ent.load_instance_entity(cid, char_id) or ent.get(char_id)
        if not char or char.get("type") != "character":
            return {"kind": "outfit", "ok": False, "error": f"{char_id} not a character", "edit": edit}
        # Bring the outfit into the instance dir if it isn't already so
        # rendering can read its coverage. This is file-management, not
        # state-mutation — the outfit dict written here is just a
        # deep-copy of the template; future edits to it would also be
        # path-based.
        outfit_inst = ent.load_instance_entity(cid, outfit_id)
        if not outfit_inst:
            template = ent.get(outfit_id)
            if not template or template.get("type") != "outfit":
                return {"kind": "outfit", "ok": False, "error": f"outfit {outfit_id} not found", "edit": edit}
            inst = copy.deepcopy(template)
            inst["_template_id"] = outfit_id
            ent.save_instance_entity(cid, inst)
            outfit_inst = inst
        presence_patch.setdefault(char_id, {})["outfit"] = outfit_id

        base_entry = {
            "kind": "outfit",
            "ok": True,
            "character_id": char_id,
            "outfit_id": outfit_id,
        }

        # v2 augmentation: if the outfit is a v2 bundle (has `equips`),
        # emit a paired patch edit that populates `worn` from the
        # bundle's equips. The outfit edit's replay handler clears
        # worn first (`effective._replay_edit`), so this patch lands
        # on an empty worn map — fresh-slate semantics matching
        # `clothing_v2.apply_outfit_preset_v2`. Without this, the
        # narrator's `[outfit char -> v2_bundle]` only updated
        # presence.outfit; worn stayed at whatever it was, and the
        # v2 renderer continued to read stale uniform pieces (or
        # whatever was previously equipped) despite the user staging
        # a different outfit.
        equips = (outfit_inst.get("properties") or {}).get("equips")
        if isinstance(equips, dict):
            new_worn = {
                slot: {"piece": piece_id, "state": "on"}
                for slot, piece_id in equips.items()
                if isinstance(slot, str) and isinstance(piece_id, str)
            }
            worn_patch = {
                "kind": "patch",
                "ok": True,
                "id": char_id,
                "data": {"properties": {"worn": new_worn}},
            }
            if _is_character_target(cid, char_id):
                return [{"kind": "cast_add", "ok": True, "id": char_id}, base_entry, worn_patch]
            return [base_entry, worn_patch]

        if _is_character_target(cid, char_id):
            return [{"kind": "cast_add", "ok": True, "id": char_id}, base_entry]
        return base_entry

    if kind == "move":
        char_id = edit.get("character_id")
        room_id = edit.get("room")
        location_id = edit.get("location")
        if not char_id or not room_id:
            return None
        # Auto-instance off-cast characters + materialize brand-new ids
        # from a generic template. The narrator-add flow's [Available
        # data]/Off-cast list lets the model emit
        # `[move rosa -> marginalia_floor]` for a character not yet in
        # the instance dir; the materialize fallback handles the
        # `[move jonah -> bedroom]` case where the narrator invents a
        # never-before-seen id (without a prior `[set jake.X = ...]`
        # patch establishing him). Both paths converge through
        # `_ensure_character_present`.
        was_new = _ensure_character_present(cid, char_id, template_hints)
        prev = dict(parent_presence.get(char_id) or {})
        # Backfill the owning location when the narrator emitted the
        # bare `[move x -> room]` form (no `location:room`). Without
        # this a brand-new NPC (no prior presence to inherit from) lands
        # with room set but location=None, and `_same_scene` — which
        # requires an exact location match — then treats that NPC as
        # "not in the scene" of any properly-located character standing
        # in the very same room, silently gating the NPC's lines out of
        # the others' prompts (the conv_7544746f1553 Leo/Bridget case).
        # Resolve room -> owning location; fall back to the character's
        # previous location if the room isn't catalogued.
        if not location_id:
            location_id = ent._find_owning_location(room_id) or prev.get("location")
        patch = presence_patch.setdefault(char_id, {})
        patch["room"] = room_id
        if location_id:
            patch["location"] = location_id
        entry = {
            "kind": "move",
            "ok": True,
            "character_id": char_id,
            "room": room_id,
            "location": location_id,
            "before": {"room": prev.get("room"), "location": prev.get("location")},
        }
        # Branch-cast-membership companion — see patch handler for the
        # full rationale. Emit cast_add for every move of a character
        # entity, not just when was_new fires, so the entity joins THIS
        # branch's cast even if a sibling branch already materialized
        # the same id.
        if _is_character_target(cid, char_id):
            return [{"kind": "cast_add", "ok": True, "id": char_id}, entry]
        return entry

    return None
