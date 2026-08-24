"""Path-based effective entity state.

The conversation tree is the source of truth. Every world-state change made
during a conversation lives on the message that caused it — narrator
edits in `metadata.applied_edits`, setup roots in `metadata.applied_edits`
seeded from `metadata.setup.edits`. Instance entity files
(`instances/<conv>/entities/<id>.json`) are baselines only — populated
once at conversation creation by deep-copying the templates, then mutated
only by deliberate studio edits (which are explicit "I want this baseline
to change everywhere").

Rendering reads `effective_entities_at(conversation, leaf_id)` instead of
the bare instance files. That walks root → leaf, accumulates every edit
on the path that targets each entity, and replays them onto a deep-copy
of the baseline. Branches stay isolated: switching to a sibling root
just swaps the path, which swaps the replay.

`effective_user_persona(conversation, leaf_id)` does the same trick for
the user persona — `settings.user_persona` is the baseline, `[set
user.X = Y]` edits along the path layer on top.
"""
from __future__ import annotations

import copy
from typing import Any

from . import entities as ent
from .merge import deep_merge


def effective_entities_at(
    conversation: dict[str, Any],
    leaf_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return {entity_id: entity} with all path-accumulated edits applied.

    Baseline = instance files. Replay = every `applied_edits` entry on
    every message from root to `leaf_id` (defaults to the conversation's
    active leaf), in chronological order. The `user` instance entity is
    handled the same way as any other entity — `[set user.X = Y]` etc.
    are regular patch edits.
    """
    cid = conversation["id"]
    baseline = ent.load_instance_entities(cid)
    out: dict[str, dict[str, Any]] = {eid: copy.deepcopy(e) for eid, e in baseline.items()}
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    if not leaf_id:
        return out

    for entry in _path_applied_edits(conversation, leaf_id):
        target = _edit_entity_id(entry)
        if not target:
            continue
        cur = out.get(target)
        if cur is None:
            # Edit references something not in the instance dir; skip.
            continue
        out[target] = _replay_edit(cur, entry)
    return out


def effective_cast_at(
    conversation: dict[str, Any],
    leaf_id: str | None = None,
) -> dict[str, set[str]]:
    """Return ``{"characters", "objects", "rooms", "locations", "outfits"}``
    id sets for the active path.

    Baseline = the conversation's instance scenario ``characters[]``,
    plus the scenario's referenced locations and the rooms reachable
    through their ``children`` (so staging-panel custom rooms that were
    spliced into a parent location's children list at conversation
    creation come along for free). Objects have NO baseline — scenario
    ``objects[]`` is the staging pool, and an object is present only
    via a path-replayed ``cast_add``. Replay = every ``cast_add`` /
    ``cast_remove`` entry on the path. The ``user`` entity is always
    considered in cast.

    If the active setup root carries ``metadata.scene_staging_picks``,
    that picks list is treated as an exclusive whitelist over the
    baseline characters. Old staging roots (created before the
    cast_add/cast_remove edits existed) only have this metadata, so
    this is what makes them behave like new ones — pick Iris and the
    branch shows just Iris even without explicit cast_remove edits.

    Branch isolation: a character added (auto-instanced or explicitly)
    on branch A but not on sibling branch B is only in A's cast set.
    Same for narrator-minted rooms / locations — they ride the
    ``cast_add`` edit ``narrator_apply._record_edit`` emits alongside
    a ``kind=replace`` that creates a brand-new entity. Scenario-
    seeded rooms / locations are visible on every branch because
    they're in the baseline.
    """
    cid = conversation["id"]
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    chars: set[str] = {"user"}
    objs: set[str] = set()
    rooms: set[str] = set()
    locations: set[str] = set()
    outfits: set[str] = set()
    scenario_id = conversation.get("scenario_id")
    if scenario_id:
        scen = ent.load_instance_entity(cid, scenario_id) or {}
        for c in (scen.get("characters") or []):
            chars.add(c)
        # Scenario `objects[]` is deliberately NOT part of the present
        # baseline: it's the staging-picker pool (what CAN be added),
        # not the scene contents. An object is only present on a branch
        # whose path carries a cast_add for it — staging pick, narrator
        # edit, or the side panel's add — and a cast_remove takes it
        # back out. This keeps objects branch-locked like everything
        # else replayed from the path.
        # Rooms + locations baseline: walk scenario.locations and pull
        # each location's room children. Staging-panel "+ Add custom
        # room" already splices new rooms into the parent location's
        # `children` list (api.py:546) so they get picked up here too.
        for lid in (scen.get("locations") or []):
            locations.add(lid)
            loc_inst = ent.load_instance_entity(cid, lid)
            for child_id in ((loc_inst or {}).get("children") or []):
                child = ent.load_instance_entity(cid, child_id)
                if child and child.get("type") == "room":
                    rooms.add(child_id)
        # Top-level orphan rooms (no parent location).
        for rid in (scen.get("rooms") or []):
            rooms.add(rid)
        # Outfits baseline: scenario top-level + custom_outfits +
        # the outfits each baseline character owns. The third bucket
        # matters because `_pull_owned_outfits` instances them into
        # the conversation at character creation without ever listing
        # them in scen.outfits. Without that walk, branch_filter
        # would hide every character's wardrobe on every branch.
        for oid in (scen.get("outfits") or []):
            outfits.add(oid)
        for outfit_data in (scen.get("custom_outfits") or []):
            if isinstance(outfit_data, dict) and outfit_data.get("id"):
                outfits.add(outfit_data["id"])
        for c in chars:
            char_inst = ent.load_instance_entity(cid, c) or {}
            char_props = char_inst.get("properties") or {}
            for oid in (char_props.get("outfits") or []):
                outfits.add(oid)
            cur = char_props.get("current_outfit")
            if cur:
                outfits.add(cur)
    if not leaf_id:
        return {
            "characters": chars,
            "objects": objs,
            "rooms": rooms,
            "locations": locations,
            "outfits": outfits,
        }
    # Scene-staging picks act as an exclusive whitelist for characters
    # on the staging-origin branch. Applied first, before cast edits,
    # so a later cast_add (e.g., narrator-instanced char) can still
    # extend the cast on that branch.
    setup_root = active_setup_root_for_path(conversation, leaf_id)
    if setup_root:
        meta = setup_root.get("metadata") or {}
        picks = (meta.get("scene_staging_picks") or {}).get("characters")
        if isinstance(picks, list):
            allow: set[str] = {p for p in picks if isinstance(p, str)} | {"user"}
            chars = {c for c in chars if c in allow}
    for entry in _path_applied_edits(conversation, leaf_id):
        kind = entry.get("kind")
        target = entry.get("id")
        if not target:
            continue
        if kind == "cast_add":
            inst = ent.load_instance_entity(cid, target)
            tmpl = ent.get(target)
            etype = (inst or tmpl or {}).get("type")
            if etype == "character":
                chars.add(target)
            elif etype == "object":
                objs.add(target)
            elif etype == "room":
                rooms.add(target)
            elif etype == "location":
                locations.add(target)
            elif etype == "outfit":
                outfits.add(target)
            else:
                # Unknown — assume character (the common case for narrator-add).
                chars.add(target)
        elif kind == "cast_remove":
            if target == "user":
                continue
            chars.discard(target)
            objs.discard(target)
            rooms.discard(target)
            locations.discard(target)
            outfits.discard(target)
    return {
        "characters": chars,
        "objects": objs,
        "rooms": rooms,
        "locations": locations,
        "outfits": outfits,
    }


def cast_removed_on_path(
    conversation: dict[str, Any],
    leaf_id: str | None = None,
) -> set[str]:
    """Return the set of entity ids that were explicitly `cast_remove`d
    somewhere on this branch's path and not re-added after. The
    narrator-add prompt surfaces this list as "Removed from this
    branch — do NOT re-add unless directive explicitly names them"
    so the model doesn't immediately re-instance characters the user
    just kicked off.
    """
    removed: set[str] = set()
    for entry in _path_applied_edits(conversation, leaf_id):
        kind = entry.get("kind")
        target = entry.get("id")
        if not target:
            continue
        if kind == "cast_remove":
            removed.add(target)
        elif kind == "cast_add":
            removed.discard(target)
    return removed


def branch_filter(
    conversation: dict[str, Any],
    leaf_id: str | None,
    entities: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Filter `entities` so only characters / objects on THIS branch's
    cast survive. Rooms, outfits, locations, scenarios, lore pass through
    untouched — those aren't branch-scoped, a room exists for the whole
    conversation regardless of which branch you're on.

    Why this matters: a character added on branch A (via a `cast_add`
    edit alongside `materialize_from_generic`) lives as an instance file
    on disk shared by every branch. `load_instance_entities` and even
    `effective_entities_at` (which uses load_instance_entities as the
    replay baseline) return that entity on sibling branch B too —
    because the file exists. But branch B's path has no `cast_add` for
    that id, so the user expectation is that the character doesn't
    exist on B.

    Callers that build a prompt or a UI view for a specific branch
    should run their entity map through this filter to keep the cast
    branch-scoped. `effective_cast_at` is the single source of truth
    for cast membership; this helper is just the convenience that
    intersects it with the entity map.

    Note: `user` is always kept (the user is always in scene). Newly-
    materialized characters surface here because they get a paired
    `cast_add` edit (see `narrator_apply._record_edit` patch handler);
    if a future code path materialises an entity WITHOUT emitting
    `cast_add`, this filter would (correctly) hide it from the branch.

    Rooms and locations are also branch-scoped: scenario-seeded ones
    (and staging-panel custom rooms, since the staging-panel splices
    them into the parent location's children) are in the baseline and
    visible on every branch; narrator-minted rooms / locations
    (created via a ``kind=replace`` edit on a single branch) carry
    a paired ``cast_add`` and only surface on the branches where that
    edit is on the path. Outfits stay permissive for now — the
    auto-instance ``[outfit char -> id]`` flow doesn't emit
    ``cast_add`` today and gating them here would hide most
    wardrobes; filtering outfits is a separate follow-up.
    """
    cast = effective_cast_at(conversation, leaf_id)
    keep_chars = set(cast.get("characters") or ()) | {"user"}
    keep_objs = set(cast.get("objects") or ())
    keep_rooms = set(cast.get("rooms") or ())
    keep_locations = set(cast.get("locations") or ())
    out: dict[str, dict[str, Any]] = {}
    for eid, ent in entities.items():
        etype = ent.get("type")
        if etype == "character":
            if eid in keep_chars:
                out[eid] = ent
        elif etype == "object":
            if eid in keep_objs:
                out[eid] = ent
        elif etype == "room":
            if eid in keep_rooms:
                out[eid] = ent
        elif etype == "location":
            if eid in keep_locations:
                out[eid] = ent
        else:
            out[eid] = ent  # outfits / scenarios / lore (permissive)
    return out


def default_responder_for_path(
    conversation: dict[str, Any],
    leaf_id: str | None = None,
) -> str | None:
    """Walk the active path leaf→root and return the most recent
    character ``speaker_id`` (skip narrator / user / system messages).
    Returns None if no character has spoken on this branch yet.
    """
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    msgs = conversation.get("messages") or {}
    cur = msgs.get(leaf_id)
    seen: set[str] = set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        sid = cur.get("speaker_id")
        persona = cur.get("persona")
        if sid and sid != "user" and persona not in ("narrator", "user", "system"):
            return sid
        if not cur.get("parent_id"):
            break
        cur = msgs.get(cur["parent_id"])
    return None


def effective_user_persona(
    conversation: dict[str, Any],
    leaf_id: str | None = None,
) -> dict[str, Any]:
    """Return the user persona dict for the given path.

    The `user` instance entity is the source of truth. Setup roots
    emit synthetic patches against it at seed time, narrator edits
    along the path (`[set user.X = Y]`) layer on top, and the picker
    populates the entity itself when the user picks a card. We
    resolve via `effective_entities_at` and flatten the entity's
    top-level fields into the flat {name, description, ...} shape
    macros / the chat UI expect.
    """
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    eff = effective_entities_at(conversation, leaf_id)
    user_entity = eff.get("user") or {}
    persona: dict[str, Any] = {
        "name": user_entity.get("name") or "User",
        "description": user_entity.get("description") or "",
    }
    for k, v in user_entity.items():
        if k in ("id", "type", "tags", "children", "properties", "_template_id", "example_text"):
            continue
        persona[k] = v
    # Preserve the picker's card_id selection (lives on settings, not
    # the entity) so the dropdown UI knows which option is selected.
    settings = conversation.get("settings") or {}
    base_settings = settings.get("user_persona") or {}
    if isinstance(base_settings, dict) and base_settings.get("card_id"):
        persona["card_id"] = base_settings["card_id"]
    return persona


def effective_scenario_instructions(
    conversation: dict[str, Any],
    leaf_id: str | None = None,
) -> str:
    """Return the scenario_instructions string for the active path.

    Setup roots stash their resolved instructions string in
    `metadata.setup.scenario_instructions`. We pick the most-recent
    setup root on the active path; otherwise fall back to
    `settings.scenario_instructions` for legacy conversations.
    """
    setup = active_setup_for_path(conversation, leaf_id)
    if setup and isinstance(setup.get("scenario_instructions"), str):
        return setup["scenario_instructions"]
    settings = conversation.get("settings") or {}
    return (settings.get("scenario_instructions") or "").strip()


def active_setup_for_path(
    conversation: dict[str, Any],
    leaf_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the most-recent setup metadata block on the active path.

    Walks leaf→root and returns the first message's `metadata.setup`
    dict it sees. Returns None if no setup root is on the path
    (legacy conversations without setups)."""
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    msgs = conversation.get("messages") or {}
    cur = msgs.get(leaf_id)
    seen: set[str] = set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        meta = cur.get("metadata") or {}
        setup = meta.get("setup")
        if isinstance(setup, dict):
            return setup
        if not cur.get("parent_id"):
            break
        cur = msgs.get(cur["parent_id"])
    return None


def active_setup_root_for_path(
    conversation: dict[str, Any],
    leaf_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the message dict (not just the metadata) of the most-recent
    setup root on the active path."""
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    msgs = conversation.get("messages") or {}
    cur = msgs.get(leaf_id)
    seen: set[str] = set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        meta = cur.get("metadata") or {}
        if isinstance(meta.get("setup"), dict):
            return cur
        if not cur.get("parent_id"):
            break
        cur = msgs.get(cur["parent_id"])
    return None


def relationships_snapshot_for_path(
    conversation: dict[str, Any],
    focal_id: str,
    target_id: str = "user",
    leaf_id: str | None = None,
) -> list[str] | None:
    """The most-recent relationship tag list for ``focal_id`` toward
    ``target_id`` on the active path.

    Walks leaf→root and returns the first `metadata.relationships[focal][target]`
    it finds — a branch-local snapshot, exactly like the setup/npc snapshots,
    so a Return-by-Death rewind to an earlier leaf naturally reads the earlier
    standing (the focal "forgets" the collapsed branch). Returns None when no
    relationship snapshot is on the path (the caller then falls back to a seed).

    This is a GENERIC core channel: any writer (a scenario seed placed on the
    root, a plain in-fiction event, or an optional module projecting its own
    state) stamps `metadata.relationships`; the core only ever reads it here and
    never learns who wrote it."""
    if not focal_id:
        return None
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    msgs = conversation.get("messages") or {}
    cur = msgs.get(leaf_id)
    seen: set[str] = set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        rel = (cur.get("metadata") or {}).get("relationships")
        if isinstance(rel, dict):
            focal_map = rel.get(focal_id)
            if isinstance(focal_map, dict) and target_id in focal_map:
                tags = focal_map.get(target_id)
                if isinstance(tags, str):
                    return [tags]
                if isinstance(tags, list):
                    return [str(t) for t in tags]
                if isinstance(tags, dict):  # {"tags": [...]} long form
                    v = tags.get("tags")
                    if isinstance(v, list):
                        return [str(t) for t in v]
        if not cur.get("parent_id"):
            break
        cur = msgs.get(cur["parent_id"])
    return None


def acquaintance_for_path(
    conversation: dict[str, Any],
    focal_id: str,
    target_id: str = "user",
    leaf_id: str | None = None,
) -> bool | None:
    """Whether ``focal_id`` KNOWS ``target_id`` (their name/identity) at the
    active leaf — an explicit perception fact, read leaf→root like the
    relationship/setup snapshots. Returns None when no fact is on the path (the
    caller then falls back to a default derived from standing).

    Separate from the presence-based locational gate: locational memory blocks
    what the focal didn't WITNESS; this blocks what the focal hasn't LEARNED
    (a stranger sees your face but not your name). Both are branch-local, so a
    Return-by-Death rewind un-learns the name for free."""
    if not focal_id:
        return None
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    msgs = conversation.get("messages") or {}
    cur = msgs.get(leaf_id)
    seen: set[str] = set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        acq = (cur.get("metadata") or {}).get("acquaintance")
        if isinstance(acq, dict):
            focal_map = acq.get(focal_id)
            if isinstance(focal_map, dict) and target_id in focal_map:
                return bool(focal_map[target_id])
        if not cur.get("parent_id"):
            break
        cur = msgs.get(cur["parent_id"])
    return None


def memory_for_path(
    conversation: dict[str, Any],
    focal_id: str,
    leaf_id: str | None = None,
) -> list[dict[str, Any]]:
    """Every fact ``focal_id`` has learned on the active path.

    Unlike the relationship/acquaintance snapshots (nearest-wins), memory
    ACCUMULATES: union every ``metadata.memory[focal]`` record from root→leaf,
    deduped by text (first occurrence kept), returned in chronological order.
    Branch-local — a Return-by-Death rewind drops facts learned on the collapsed
    branch, so the character forgets exactly the loop."""
    if not focal_id:
        return []
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    msgs = conversation.get("messages") or {}
    chain: list[dict[str, Any]] = []
    cur = msgs.get(leaf_id)
    seen_ids: set[str] = set()
    while cur and cur["id"] not in seen_ids:
        seen_ids.add(cur["id"])
        chain.append(cur)
        if not cur.get("parent_id"):
            break
        cur = msgs.get(cur["parent_id"])
    # Gather every record on the path (chronological), then resolve
    # consolidation: a record's `supersedes` names an earlier fact that no
    # longer holds (a password/plan changed). Dedup is by normalized text, so
    # near-duplicate wordings collapse too.
    from .memory import normalize as _norm
    records: list[dict[str, Any]] = []
    for m in reversed(chain):  # root → leaf, chronological
        mem = (m.get("metadata") or {}).get("memory")
        if not isinstance(mem, dict):
            continue
        for rec in (mem.get(focal_id) or []):
            if isinstance(rec, dict) and (rec.get("text") or "").strip():
                records.append(rec)
    superseded = {_norm(r["supersedes"]) for r in records
                  if isinstance(r.get("supersedes"), str) and r["supersedes"].strip()}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rec in records:
        key = _norm(rec.get("text") or "")
        if not key or key in seen or key in superseded:
            continue
        seen.add(key)
        out.append(rec)
    return out


def _replay_seq(entity: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a sequence of edits onto an entity via the pure _replay_edit.
    `_replay_edit` deep-copies internally, so `entity` is never mutated."""
    out = entity
    for e in entries:
        out = _replay_edit(out, e)
    return out


def path_active_edit_keys(
    conversation: dict[str, Any], leaf_id: str | None = None
) -> set[tuple[str, int]]:
    """Return the set of ``(message_id, index)`` for edits whose effect is
    still present in the current effective state on this path — i.e. the
    "Active" edits.

    An edit is Active iff it currently changes the branch's state:
      * entity edits (patch / replace / unset / outfit) — Active iff
        replaying that entity's live edits WITHOUT this one yields a
        different entity than WITH it. Uses the real `_replay_edit`, so
        supersession, nested unsets and deep-merge overrides are all
        handled correctly (a later write that fully overrides an earlier
        one makes the earlier one inactive; an `unset` that removes a
        still-absent field stays active).
      * ``move`` — Active iff it's the last non-reverted move for that
        character (earlier moves are superseded).
      * ``cast_add`` / ``cast_remove`` — Active iff it's the last
        non-reverted cast op for that id.

    Reverted (``reverted_at``) and failed (``ok is False``) edits are never
    Active. Edits whose target has no baseline (never materialized) are
    skipped, mirroring `effective_entities_at`.
    """
    from collections import defaultdict

    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    active: set[tuple[str, int]] = set()
    if not leaf_id:
        return active

    msgs = conversation.get("messages") or {}
    chain: list[dict[str, Any]] = []
    cur = msgs.get(leaf_id)
    seen: set[str] = set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        chain.append(cur)
        if not cur.get("parent_id"):
            break
        cur = msgs.get(cur["parent_id"])
    chain.reverse()  # root -> leaf (chronological)

    entity_recs: dict[str, list[tuple[tuple[str, int], dict[str, Any]]]] = defaultdict(list)
    last_move: dict[str, tuple[str, int]] = {}
    last_cast: dict[str, tuple[str, int]] = {}
    for m in chain:
        log = (m.get("metadata") or {}).get("applied_edits") or []
        for idx, e in enumerate(log):
            if not isinstance(e, dict) or e.get("reverted_at") or e.get("ok") is False:
                continue
            key = (m["id"], idx)
            kind = e.get("kind")
            if kind == "move":
                cid_ = e.get("character_id")
                if cid_:
                    last_move[cid_] = key
                continue
            if kind in ("cast_add", "cast_remove"):
                id_ = e.get("id")
                if id_:
                    last_cast[id_] = key
                continue
            tgt = _edit_entity_id(e)
            if tgt is not None:
                entity_recs[tgt].append((key, e))

    active.update(last_move.values())
    active.update(last_cast.values())

    baseline = ent.load_instance_entities(conversation["id"])
    for tgt, recs in entity_recs.items():
        base = baseline.get(tgt)
        if base is None:
            continue  # no baseline -> effective_entities_at skips it -> inactive
        seq = [e for _k, e in recs]
        full = _replay_seq(base, seq)
        for i, (key, _e) in enumerate(recs):
            reduced = _replay_seq(base, [seq[j] for j in range(len(seq)) if j != i])
            if reduced != full:
                active.add(key)
    return active


def path_applied_edits_with_origin(
    conversation: dict[str, Any], leaf_id: str | None = None
) -> list[dict[str, Any]]:
    """Return every applied-edit entry on the path, decorated with the
    message id + persona that produced it, plus `_active` (effect still
    present in the current branch state) and `_made_at` (when the edit was
    made — its own `made_at`, falling back to the source message's
    `created_at` for edits recorded before timestamps were added).
    Most-recent first.

    Used by the chat UI's Applied-edits timeline so the user can see every
    world-state change in one place, split into Active vs. all-on-branch.
    """
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    active_keys = path_active_edit_keys(conversation, leaf_id)
    msgs = conversation.get("messages") or {}
    chain: list[dict[str, Any]] = []
    cur = msgs.get(leaf_id)
    seen: set[str] = set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        chain.append(cur)
        if not cur.get("parent_id"):
            break
        cur = msgs.get(cur["parent_id"])
    out: list[dict[str, Any]] = []
    for m in chain:
        meta = m.get("metadata") or {}
        log = meta.get("applied_edits") or []
        if not log:
            continue
        is_setup = isinstance(meta.get("setup"), dict)
        for idx, entry in enumerate(log):
            decorated = dict(entry)
            decorated["_message_id"] = m["id"]
            decorated["_persona"] = m.get("persona") or "narrator"
            decorated["_index"] = idx
            decorated["_origin"] = "setup" if is_setup else "narrator"
            decorated["_active"] = (m["id"], idx) in active_keys
            decorated["_made_at"] = entry.get("made_at") or m.get("created_at")
            out.append(decorated)
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _path_applied_edits(
    conversation: dict[str, Any], leaf_id: str
) -> list[dict[str, Any]]:
    """Return every applied-edit entry on the path root→leaf, in order."""
    msgs = conversation.get("messages") or {}
    chain: list[dict[str, Any]] = []
    cur = msgs.get(leaf_id)
    seen: set[str] = set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        chain.append(cur)
        if not cur.get("parent_id"):
            break
        cur = msgs.get(cur["parent_id"])
    chain.reverse()
    out: list[dict[str, Any]] = []
    for m in chain:
        log = (m.get("metadata") or {}).get("applied_edits") or []
        for entry in log:
            if entry.get("ok") is False:
                continue
            if entry.get("reverted_at"):
                continue
            out.append(entry)
    return out


def _edit_entity_id(entry: dict[str, Any]) -> str | None:
    kind = entry.get("kind")
    if kind in ("patch", "replace", "set", "unset"):
        return entry.get("id")
    if kind == "outfit":
        return entry.get("character_id")
    # cast_add / cast_remove are cast-membership only — they don't
    # mutate any entity, so effective_entities_at skips them. The
    # branch cast set is computed separately by effective_cast_at.
    return None


def _replay_edit(entity: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Apply one applied-edit entry to a deep-copy of `entity` and
    return the result. Pure function — does not touch any files."""
    kind = entry.get("kind")
    out = copy.deepcopy(entity)
    if kind == "patch":
        data = entry.get("data") or {}
        if isinstance(data, dict):
            deep_merge(out, data)
        return out
    if kind == "replace":
        data = entry.get("data") or {}
        if isinstance(data, dict):
            return {**data, "id": entry.get("id") or out.get("id")}
        return out
    if kind == "unset":
        path = entry.get("path") or []
        if not path:
            return out
        cur: Any = out
        for key in path[:-1]:
            if not isinstance(cur, dict) or key not in cur:
                return out
            cur = cur[key]
        if isinstance(cur, dict):
            cur.pop(path[-1], None)
        return out
    if kind == "outfit":
        outfit_id = entry.get("outfit_id")
        if outfit_id:
            props = out.setdefault("properties", {})
            props["current_outfit"] = outfit_id
            # An outfit swap is "fresh slate" semantics — drop any
            # per-slot overrides set against the prior outfit so
            # `[outfit -> X]` returns the wardrobe to X's preset slots
            # without dragging in a stale `[set ...clothing_overrides...]`
            # from a previous turn. Same for clothing_transparency: a
            # 50% see-through shirt shouldn't carry over to a wholly
            # different outfit.
            props.pop("clothing_overrides", None)
            props.pop("clothing_transparency", None)
            # v2: clear worn so the paired patch edit (emitted by
            # narrator_apply._record_edit when the target outfit is a
            # v2 bundle) can populate fresh slot map from the new
            # bundle's equips. No-op for v1 characters (no worn
            # field). For v2-character-receiving-v1-outfit, this
            # leaves worn empty (renders as unclothed under v2 path) —
            # rare case; means the data is mixing v1 and v2 outfits.
            if "worn" in props:
                props["worn"] = {}
        return out
    return out


