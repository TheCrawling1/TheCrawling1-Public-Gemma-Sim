"""Tree-based conversations with per-message presence snapshots.

Each conversation is a folder under instances/<id>/ containing:
  conversation.json   message tree + metadata + per-conversation settings
  entities/           deep-copied scenario entities, mutated only by this convo
"""
from __future__ import annotations

import copy
import time
import uuid
from pathlib import Path
from typing import Any

from flask import current_app

from .entities import (
    create_instance_from_scenario,
    instance_root,
    instances_dir,
)
from .macros import apply as apply_macros
from .narrator_apply import apply_edits as _apply_narrator_edits
from .setups import (
    apply_user_persona_edits as _apply_user_edits,
    compute_presence as _compute_presence,
    parse_setup_state as _parse_setup_state,
    resolve_setup as _resolve_setup,
    setup_list as _setup_list,
)
from .storage import delete_path, list_json_files, read_json, write_json
from . import modules as _modules


def _module_default_settings(module_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Seed ``metadata.module_settings`` with each active module's manifest
    defaults so the chat UI has something to render before any custom
    edits arrive. Unknown ids are silently dropped."""
    out: dict[str, dict[str, Any]] = {}
    for mid in module_ids:
        manifest = _modules.get(mid)
        if not manifest:
            continue
        out[mid] = _modules.default_setting_values(manifest)
    return out


# ---------------------------------------------------------------------------
# Conversation file plumbing
# ---------------------------------------------------------------------------


def _conv_file(conversation_id: str) -> Path:
    return instance_root(conversation_id) / "conversation.json"


def list_conversations() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    base = instances_dir()
    if not base.exists():
        return out
    for child in sorted(base.iterdir()):
        cf = child / "conversation.json"
        if cf.is_file():
            try:
                conv = read_json(cf)
            except Exception:
                continue
            out.append(
                {
                    "id": conv.get("id", child.name),
                    "title": conv.get("title", child.name),
                    "scenario_id": conv.get("scenario_id"),
                    "created_at": conv.get("created_at", 0),
                    "updated_at": conv.get("updated_at", 0),
                    "message_count": len(conv.get("messages", {})),
                }
            )
    out.sort(key=lambda c: c["updated_at"] or c["created_at"], reverse=True)
    return out


def load_conversation(conversation_id: str) -> dict[str, Any] | None:
    conv = read_json(_conv_file(conversation_id))
    if conv is not None and "branch_choices" not in conv:
        # Backfill for conversations created before the branch-memory was
        # added so the existing active path is remembered the first time
        # the user navigates siblings.
        conv["branch_choices"] = {}
        leaf = conv.get("active_path_leaf")
        if leaf:
            record_branch_choice_path(conv, leaf)
    return conv


def save_conversation(conversation: dict[str, Any]) -> None:
    conversation["updated_at"] = int(time.time())
    write_json(_conv_file(conversation["id"]), conversation)


def delete_conversation(conversation_id: str) -> None:
    delete_path(instance_root(conversation_id))


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def new_conversation_id() -> str:
    return f"conv_{uuid.uuid4().hex[:12]}"


def reseed_from_scenario(
    conversation_id: str,
    active_setup_id: str | None = None,
    *,
    start_toggles: dict[str, bool] | None = None,
    random_pick_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Wipe all messages + the running summary from an existing conversation
    and re-seed sibling root setups + first-message greetings from the
    scenario.

    The conversation id, title, settings (other than summary +
    scenario_instructions + user_persona, which are reset to the active
    setup's values), and existing instance entities are preserved. The
    instance is NOT re-instanced — narrator edits made mid-conversation
    survive — but the active setup's `state` directives are re-applied
    on top of whatever's there.

    `random_pick_overrides` lets a re-roll button on the active setup
    root push new picks (e.g. {"partner": "sable"}). The mutated
    scenario file in the instance dir is updated to match — same flow
    as `create_conversation_from_scenario`.
    """
    from .entities import load_all
    conv = load_conversation(conversation_id)
    if not conv:
        raise ValueError(f"Conversation {conversation_id!r} not found.")
    scenario_id = conv.get("scenario_id")
    if not scenario_id:
        raise ValueError("Conversation has no scenario_id; can't reseed.")

    all_entities = load_all()
    scenario = all_entities.get(scenario_id)
    if not scenario or scenario.get("type") != "scenario":
        raise ValueError(f"Scenario {scenario_id!r} not found.")
    # Operate on a deep copy — we may mutate `scenario.characters[]` /
    # `scenario.objects[]` to record the new random picks, and we don't
    # want to clobber the in-memory cache that other requests share.
    import copy as _copy
    scenario = _copy.deepcopy(scenario)

    # Roll random picks the same way create_conversation_from_scenario
    # does. Empty pool → no-op; overrides win over a fresh roll.
    from .setups import roll_random_picks
    random_picks = roll_random_picks(
        scenario,
        overrides=random_pick_overrides,
        toggles=start_toggles,
    )
    if random_picks.get("partner"):
        chars = list(scenario.get("characters") or [])
        if random_picks["partner"] not in chars:
            chars.append(random_picks["partner"])
        scenario["characters"] = chars
    if random_picks.get("magic_item"):
        objs = list(scenario.get("objects") or [])
        if random_picks["magic_item"] not in objs:
            objs.append(random_picks["magic_item"])
        scenario["objects"] = objs
    # Persist the mutated scenario into the instance dir so a reload
    # picks up the resolved cast.
    if random_picks.get("partner") or random_picks.get("magic_item"):
        from .entities import write_json, instance_entities_dir
        write_json(
            instance_entities_dir(conversation_id) / f"{scenario_id}.json",
            scenario,
        )

    settings = conv.setdefault("settings", {})
    settings["summary"] = ""
    settings["summary_anchor_ids"] = []
    settings.setdefault("user_persona", {"name": "User", "description": ""})

    conv["messages"] = {}
    conv["branch_choices"] = {}

    _seed_setup_roots(
        conv,
        scenario,
        all_entities,
        active_setup_id=active_setup_id,
        is_reseed=True,
        random_picks=random_picks,
        start_toggles=start_toggles or {},
    )

    save_conversation(conv)
    return conv


def create_conversation_from_scenario(
    scenario_id: str,
    title: str | None = None,
    active_setup_id: str | None = None,
    *,
    start_toggles: dict[str, bool] | None = None,
    random_pick_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    cfg = current_app.config
    defaults = cfg.get("defaults", {}) or {}
    conv_id = new_conversation_id()
    instance = create_instance_from_scenario(scenario_id, conv_id)
    scenario = instance["entities"].get(scenario_id, {})

    # Roll random partner / magic-item picks against the scenario's pools.
    # Overrides win (deterministic tests, explicit user choice from the
    # pre-creation modal); otherwise we pick uniformly at random.
    from .setups import roll_random_picks
    random_picks = roll_random_picks(
        scenario,
        overrides=random_pick_overrides,
        toggles=start_toggles,
    )
    # Stamp picked entities into the scenario's `characters` / `objects`
    # lists so existing downstream code (turn_order, instance pool,
    # first_messages walker) treats them as in-scene without special-
    # casing the random pool. The instance file is then updated to match
    # so the conversation reads consistently on reload.
    # Stamp the picked partner / item into the scenario's cast lists.
    # Pool members are no longer pre-instanced at creation; we need to
    # explicitly deep-copy each picked id into the instance so the
    # downstream code (presence, prompt, cast widgets) finds them.
    from .entities import (
        write_json, instance_entities_dir, save_instance_entity, get as _ent_get,
    )
    if random_picks.get("partner"):
        chars = list(scenario.get("characters") or [])
        if random_picks["partner"] not in chars:
            chars.append(random_picks["partner"])
        scenario["characters"] = chars
        # Instance the partner template if not already.
        if random_picks["partner"] not in instance["entities"]:
            tmpl = _ent_get(random_picks["partner"])
            if tmpl:
                copied = copy.deepcopy(tmpl)
                copied["_template_id"] = random_picks["partner"]
                save_instance_entity(conv_id, copied)
                instance["entities"][random_picks["partner"]] = copied
                # Also pull the partner's outfits + extends chain so a
                # later /outfit edit doesn't 404 on a missing template.
                tmpl_props = tmpl.get("properties") or {}
                outfit_ids = list(tmpl_props.get("outfits") or [])
                if tmpl_props.get("current_outfit"):
                    outfit_ids.append(tmpl_props["current_outfit"])
                for oid in outfit_ids:
                    if oid in instance["entities"]:
                        continue
                    o_tmpl = _ent_get(oid)
                    if not o_tmpl:
                        continue
                    o_copy = copy.deepcopy(o_tmpl)
                    o_copy["_template_id"] = oid
                    save_instance_entity(conv_id, o_copy)
                    instance["entities"][oid] = o_copy
    if random_picks.get("magic_item"):
        objs = list(scenario.get("objects") or [])
        if random_picks["magic_item"] not in objs:
            objs.append(random_picks["magic_item"])
        scenario["objects"] = objs
        if random_picks["magic_item"] not in instance["entities"]:
            tmpl = _ent_get(random_picks["magic_item"])
            if tmpl:
                copied = copy.deepcopy(tmpl)
                copied["_template_id"] = random_picks["magic_item"]
                save_instance_entity(conv_id, copied)
                instance["entities"][random_picks["magic_item"]] = copied
    # Persist the mutated scenario into the instance dir so a reload
    # picks up the resolved cast.
    if random_picks.get("partner") or random_picks.get("magic_item"):
        write_json(
            instance_entities_dir(conv_id) / f"{scenario_id}.json", scenario
        )

    now = int(time.time())

    conversation = {
        "id": conv_id,
        "title": title or scenario.get("name") or "New conversation",
        "scenario_id": scenario_id,
        "created_at": now,
        "updated_at": now,
        "messages": {},
        "active_path_leaf": "",
        # Per-fork "last active child" memo. When the user navigates back to
        # a sibling that has descendants, we descend through this map to
        # restore the leaf they were on inside that subtree, so a branch
        # never appears to swallow the messages below the fork point.
        "branch_choices": {},
        "settings": {
            "context_limit_tokens": scenario.get(
                "context_limit_tokens", defaults.get("context_limit_tokens", 8000)
            ),
            "locational_memory": defaults.get("locational_memory", True),
            "narrator_mode": scenario.get(
                "narrator_mode", defaults.get("narrator_mode", "auto")
            ),
            "turn_mode": scenario.get(
                "turn_mode", defaults.get("turn_mode", "manual")
            ),
            "turn_order": list(scenario.get("characters", []) or []),
            "turn_index": 0,
            "dev_panel_instructions": "",
            "ollama_model_override": None,
            "user_persona": {"name": "User", "description": ""},
            "author_note": "",
            "author_note_depth": 1,
            "author_note_per_character": {},
            "post_history_instructions": "",
            "system_prompt_character": "",
            "system_prompt_narrator": "",
            "summary": "",
            "auto_responder_by_mention": scenario.get(
                "auto_responder_by_mention",
                defaults.get("auto_responder_by_mention", False),
            ),
            "scenario_instructions": "",
            "multi_response": False,
            "multi_response_excluded": [],
        },
    }

    _seed_setup_roots(
        conversation,
        scenario,
        instance["entities"],
        active_setup_id=active_setup_id,
        is_reseed=False,
        baseline_starting_state=instance["starting_state"],
        random_picks=random_picks,
        start_toggles=start_toggles or {},
    )

    save_conversation(conversation)
    return conversation


# ---------------------------------------------------------------------------
# Setup seeding
# ---------------------------------------------------------------------------


def _seed_setup_roots(
    conversation: dict[str, Any],
    scenario: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    *,
    active_setup_id: str | None,
    is_reseed: bool,
    baseline_starting_state: dict[str, Any] | None = None,
    random_picks: dict[str, str | None] | None = None,
    start_toggles: dict[str, bool] | None = None,
) -> None:
    """Create one sibling root narrator message per setup, then hang
    first-message greetings off whichever root is the active setup.

    The active setup's `state` edits are applied to the instance via the
    every setup root carries its full edit list in
    `metadata.applied_edits` (same shape narrator-edits use). The
    rendering layer walks the active path and replays those edits onto
    the instance baseline, so each setup's state is naturally isolated
    to its own branch — switching to a sibling root just changes the
    path that gets replayed.

    `metadata.setup` holds the non-replay setup metadata (id, name,
    description, scenario_instructions, user_persona) so the chat UI
    can label the root and `effective_scenario_instructions` /
    `effective_user_persona` can pick them up.
    """
    setups = _setup_list(scenario)
    if not setups:
        return

    # Substitute {{partner}} / {{magic_item}} macros into the resolved
    # setups using the picks rolled in `create_conversation_from_scenario`.
    # Done in-place on the resolved dicts so every downstream consumer
    # (state edits, opening prompt, instructions, first_messages keys)
    # sees the substituted strings without further plumbing.
    if random_picks:
        from .setups import substitute_macros
        partner_id = random_picks.get("partner")
        partner_name = None
        if partner_id and partner_id in entities:
            partner_name = (entities[partner_id].get("name") or partner_id)
        magic_item_id = random_picks.get("magic_item")
        magic_item_name = None
        if magic_item_id and magic_item_id in entities:
            magic_item_name = (entities[magic_item_id].get("name") or magic_item_id)

        def _sub(text: str) -> str:
            return substitute_macros(
                text,
                partner_id=partner_id,
                partner_name=partner_name,
                magic_item_id=magic_item_id,
                magic_item_name=magic_item_name,
            )

        for setup in setups:
            for key in (
                "state", "opening_prompt", "scenario_instructions",
                "scenario_instructions_base", "scenario_instructions_append",
                "system_prompt_character", "system_prompt_narrator",
                "author_note", "post_history_instructions",
            ):
                if isinstance(setup.get(key), str):
                    setup[key] = _sub(setup[key])
            fm = setup.get("first_messages") or {}
            if isinstance(fm, dict):
                setup["first_messages"] = {
                    _sub(k): _sub(v) for k, v in fm.items()
                    if isinstance(k, str) and isinstance(v, str)
                }

    # Pick the active setup. Falls back to the first setup if the requested
    # id isn't present.
    active_idx = 0
    if active_setup_id:
        for i, s in enumerate(setups):
            if s["id"] == active_setup_id:
                active_idx = i
                break
    active = setups[active_idx]

    # Baseline presence: scenario.starting_state shaped as a presence_snapshot.
    if baseline_starting_state is not None:
        baseline = baseline_starting_state
    else:
        baseline = _baseline_presence_from_scenario(scenario, entities)

    settings = conversation.setdefault("settings", {})
    # Settings carry the active setup's resolved values for any caller
    # that doesn't go through `effective_*` (studio displays, exports).
    # Path-replay is still authoritative at render time.
    settings["scenario_instructions"] = active["scenario_instructions"]
    settings.setdefault("user_persona", {"name": "User", "description": ""})
    settings["user_persona"] = dict(active["user_persona"])
    # Pre-fill prompt fields from the scenario / active setup so the
    # left-panel textareas show the actual text being injected. Empty
    # values fall through to the built-in defaults at render time.
    if active.get("system_prompt_character"):
        settings["system_prompt_character"] = active["system_prompt_character"]
    if active.get("system_prompt_narrator"):
        settings["system_prompt_narrator"] = active["system_prompt_narrator"]
    if active.get("author_note"):
        settings["author_note"] = active["author_note"]
    if active.get("author_note_depth") is not None:
        settings["author_note_depth"] = active["author_note_depth"]
    if active.get("author_note_per_character"):
        settings["author_note_per_character"] = active["author_note_per_character"]
    if active.get("post_history_instructions"):
        settings["post_history_instructions"] = active["post_history_instructions"]

    now = int(time.time())
    last_leaf = ""

    # Setup-picker gate: scenarios that opt in via `setup_picker: true`
    # at the top level use the staging-panel UX — setup roots have
    # empty content, first_messages are deferred until the user clicks
    # Start in the staging panel. Every other scenario keeps the
    # legacy flow: setup root content = opening_prompt, first_messages
    # seeded immediately. The flag lives on the scenario, so existing
    # legacy scenarios (the_marginalia, corner_cafe, etc.)
    # are unaffected.
    use_picker = bool(scenario.get("setup_picker"))

    from . import setups as _setups_mod

    for idx, setup in enumerate(setups):
        is_active = idx == active_idx
        scene_fields = _setups_mod.scene_staging_fields(setup)
        is_scene_staging = scene_fields is not None
        # Either the scenario-level setup_picker (trap) or the per-setup
        # scene_staging_fields gate defers content + first_messages
        # until the user clicks Start in the panel.
        defer_content = use_picker or is_scene_staging
        user_edits, entity_edits = _parse_setup_state(setup["state"])

        # Materialize the resolved user_persona (name / description /
        # arbitrary keys) as patch edits against the `user` entity so
        # the path-replay reader treats them like any other narrator
        # edit. This means a setup author can write
        # `"user_persona": {"name": "Producer-san"}` instead of `[set
        # user.name = "Producer-san"]` and still get the same end
        # state on the user entity.
        persona_overrides = setup.get("user_persona") or {}
        if isinstance(persona_overrides, dict):
            for key, value in persona_overrides.items():
                if value is None:
                    continue
                # Skip the "User" / "" placeholders that the legacy
                # default-setup synthesizer emits — those would
                # clobber a picked persona's data with empty strings.
                if key == "name" and value in ("", "User"):
                    continue
                if key == "description" and value == "":
                    continue
                user_edits.append({
                    "kind": "patch",
                    "id": "user",
                    "data": _nest_value(key, value),
                })

        # Run the edits through the recorder so the applied_edits log on
        # the root has the canonical shape the path-replay reader expects.
        presence_patch, applied_log = _apply_narrator_edits(
            conversation["id"],
            entity_edits + user_edits,
            {"presence": dict(baseline.get("presence") or {})},
        )
        snap = _merge_presence_patch(baseline, presence_patch)

        root_id = f"msg_{uuid.uuid4().hex[:10]}"
        meta: dict[str, Any] = {
            "opening": True,
            "setup": {
                "id": setup["id"],
                "name": setup["name"],
                "description": setup["description"],
                "scenario_instructions": setup["scenario_instructions"],
                "scenario_instructions_base": setup.get("scenario_instructions_base", ""),
                "scenario_instructions_append": setup.get("scenario_instructions_append", ""),
                "user_persona": dict(setup["user_persona"]),
                "state": setup["state"],
                "system_prompt_character": setup.get("system_prompt_character", ""),
                "system_prompt_narrator": setup.get("system_prompt_narrator", ""),
                "author_note": setup.get("author_note", ""),
                "author_note_depth": setup.get("author_note_depth"),
                "author_note_per_character": dict(setup.get("author_note_per_character") or {}),
                "post_history_instructions": setup.get("post_history_instructions", ""),
                "available_modules": list(setup.get("available_modules") or []),
                "default_modules": list(setup.get("default_modules") or []),
            },
            # Pre-seed the active module list with the setup's defaults
            # and per-module settings with each manifest's defaults so
            # the chat UI has something to render before any Scene
            # staging Start fires. Scene-staging Start overwrites both
            # with the user's panel picks; non-staging setups keep
            # these defaults verbatim.
            "modules": list(setup.get("default_modules") or []),
            "module_settings": _module_default_settings(
                setup.get("default_modules") or []
            ),
            # Active quests for this branch (read by the pf1e quest engine),
            # seeded like modules. quest_giver names who judges an accusation.
            "quests": list(setup.get("default_quests") or []),
        }
        if setup.get("quest_giver"):
            meta["quest_giver"] = setup["quest_giver"]
        if isinstance(setup.get("grid"), dict):
            meta["grid"] = setup["grid"]
        # A cast-sourced on-demand grid: no static units, the encounter is
        # seeded from the pf1e actors present in the room when the player
        # enters the grid (a text-first scene that CAN escalate to combat).
        if isinstance(setup.get("grid_from_cast"), dict):
            meta["grid_from_cast"] = setup["grid_from_cast"]
        if isinstance(setup.get("shop"), dict):
            meta["shop"] = setup["shop"]
        if isinstance(setup.get("npc_sim"), dict):
            meta["npc_sim"] = setup["npc_sim"]
        if applied_log:
            meta["applied_edits"] = applied_log
        if is_reseed:
            meta["reseeded"] = True
        if use_picker:
            # Setup-picker / staging mode: the chat UI hangs a prep panel
            # below this root so the user can swap partner, add or remove
            # items, and press Start. The narrator opening prose lives on
            # metadata.setup.opening_prompt and gets persisted as a child
            # narrator message when /scenario-prep/start fires. The panel
            # auto-hides once any child exists.
            meta["staging"] = True
            meta["setup"]["opening_prompt"] = setup["opening_prompt"]
        if is_scene_staging:
            # Per-setup Scene staging gate: the chat UI hangs the Scene
            # staging panel below this root. Picks land via
            # /scenario-prep/scene-stage which spawns a child narrator
            # branch with the user's prompt + presence. The panel stays
            # rendered after children exist so the user can re-stage
            # against the same root, each click producing a new sibling
            # branch off the staging root.
            meta["scene_staging"] = True
            meta["scene_staging_fields"] = scene_fields
            meta["setup"]["opening_prompt"] = setup.get("opening_prompt") or ""
            # Carry the resolved first_messages so start_scene_staging
            # can hang per-character greetings off the new branch
            # without having to re-resolve the scenario file.
            meta["setup"]["first_messages"] = dict(setup.get("first_messages") or {})
        if is_active:
            meta["setup_active"] = True
            # Record the rolled picks + toggle choices on the active root
            # so the chat UI can show "rolled: Cosmo + BBC Bracelet" and a
            # future re-roll command has the inputs to replay against.
            if random_picks and any(random_picks.values()):
                meta["random_picks"] = {
                    k: v for k, v in random_picks.items() if v
                }
            if start_toggles:
                meta["start_toggles"] = dict(start_toggles)
        conversation["messages"][root_id] = {
            "id": root_id,
            "parent_id": None,
            "persona": "narrator",
            "speaker_id": None,
            # Picker mode: empty content (the prep panel attachment is
            # the visible UI; opening prose appends as a child on Start).
            # Legacy mode: opening_prompt as content, first_messages
            # seeded immediately below — the standard flow every
            # scenario used before the trap setup_picker was added.
            "content": "" if defer_content else setup["opening_prompt"],
            "presence_snapshot": snap,
            "created_at": now,
            "edited_at": None,
            "metadata": meta,
        }
        if is_active:
            if defer_content:
                last_leaf = root_id
            else:
                # Legacy: hang the per-character first_message greetings
                # off the active root so the conversation is immediately
                # playable on first open — no Start step.
                leaf = _seed_first_message_chain(
                    conversation, scenario, entities, root_id, snap, setup, is_reseed,
                )
                last_leaf = leaf or root_id
                # A setup may open straight into a tactical-grid encounter: hang a
                # Grid Message node off the opening so the fight is a rewindable
                # block (the pf1e module seeds the field from it and, on finish,
                # appends a Grid Response). Text before it stays a rewind point.
                gm = setup.get("grid_message")
                if isinstance(gm, dict):
                    last_leaf = _seed_grid_message(
                        conversation, last_leaf, snap, gm, is_reseed,
                    )

    conversation["active_path_leaf"] = last_leaf or next(iter(conversation["messages"]))
    record_branch_choice_path(conversation, conversation["active_path_leaf"])


def propagate_partner_to_staging_roots(
    conv: dict[str, Any],
    scenario: dict[str, Any],
    partner_id: str,
    partner_name: str | None = None,
) -> bool:
    """Update every staging root's metadata so the new partner is
    consistently named in setup.opening_prompt / scenario_instructions /
    state across all setups (not just the active one).

    Without this, swapping the partner on the roommate setup leaves
    the date setup's metadata pinned to the original roll — and a
    user who navigates to date via the setup dropdown would see the
    sidebar show stale text. The dropdown-reroll route, the Library
    +/- handler, and start_staging all funnel through this helper.

    Returns True when at least one staging root was updated.
    """
    if not partner_id:
        return False
    if partner_name is None:
        from .entities import load_instance_entity
        # Best-effort name lookup; fall back to id when entity not yet
        # instanced (the caller will usually instance it before calling).
        # Skip if no conversation id available — happens in tests.
        cid = conv.get("id")
        if cid:
            ent = load_instance_entity(cid, partner_id) or {}
            partner_name = ent.get("name") or partner_id
        else:
            partner_name = partner_id

    from .setups import setup_list, substitute_macros
    setups = setup_list(scenario)
    changed = False
    for root in conv.get("messages", {}).values():
        sm = root.get("metadata") or {}
        if not sm.get("staging") or root.get("parent_id") is not None:
            continue
        this_setup_id = (sm.get("setup") or {}).get("id")
        this_setup = next((s for s in setups if s["id"] == this_setup_id), None)
        if not this_setup:
            continue
        # Update random_picks.partner
        rp = sm.setdefault("random_picks", {})
        if rp.get("partner") != partner_id:
            rp["partner"] = partner_id
            changed = True
        # Re-substitute macros on metadata.setup.*. The setup_list
        # values are the un-substituted master text, so each call
        # always gets a fresh substitution against the new partner.
        su_meta = sm.setdefault("setup", {})
        for k in ("opening_prompt", "scenario_instructions",
                  "scenario_instructions_base",
                  "scenario_instructions_append", "state"):
            src = this_setup.get(k)
            if isinstance(src, str):
                new_val = substitute_macros(
                    src, partner_id=partner_id, partner_name=partner_name,
                )
                if su_meta.get(k) != new_val:
                    su_meta[k] = new_val
                    changed = True
    return changed


def start_staging(
    conversation_id: str,
    *,
    setup_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a staging setup root into the actual scene: appends the
    narrator opening prose as a child of the staging root, then hangs
    the per-character first_message greetings off it. The staging
    panel hides automatically once children exist.

    The opening prose is re-substituted with the current
    metadata.random_picks (so a partner swap during staging propagates
    into the prose). Existing children of the staging root are left
    alone — calling this twice on the same root just appends another
    sibling chain off the root, navigable via the existing branch
    chips, so a re-stage works the same way a regen does.

    `setup_id` picks which staging root to start; defaults to the one
    flagged setup_active. Raises ValueError when the root or scenario
    can't be found.
    """
    conv = load_conversation(conversation_id)
    if not conv:
        raise ValueError(f"Conversation {conversation_id!r} not found.")
    scenario_id = conv.get("scenario_id")
    if not scenario_id:
        raise ValueError("Conversation has no scenario_id.")

    # Find the target staging root.
    target_root = None
    for m in conv.get("messages", {}).values():
        meta = m.get("metadata") or {}
        if not meta.get("staging") or m.get("parent_id") is not None:
            continue
        if setup_id:
            if (meta.get("setup") or {}).get("id") == setup_id:
                target_root = m
                break
        elif meta.get("setup_active"):
            target_root = m
            break
    if not target_root and not setup_id:
        # Fall back to first staging root.
        for m in conv.get("messages", {}).values():
            meta = m.get("metadata") or {}
            if meta.get("staging") and m.get("parent_id") is None:
                target_root = m
                break
    if not target_root:
        raise ValueError("No staging root found.")

    from .entities import load_instance_entities, load_all
    # Load instance entities so the scenario reflects post-staging
    # mutations (partner swap rewrites scenario.characters[]).
    instance_ents = load_instance_entities(conversation_id) or {}
    scenario = instance_ents.get(scenario_id) or load_all().get(scenario_id)
    if not scenario:
        raise ValueError(f"Scenario {scenario_id!r} not found.")

    # Re-resolve setup with macro substitution against the CURRENT picks
    # on the target root (partner may have changed during staging).
    from .setups import setup_list, substitute_macros
    setups = setup_list(scenario)
    target_setup_id = (target_root.get("metadata") or {}).get("setup", {}).get("id")
    setup = next((s for s in setups if s["id"] == target_setup_id), None)
    if not setup:
        raise ValueError(f"Setup {target_setup_id!r} not in scenario.")

    picks = (target_root.get("metadata") or {}).get("random_picks") or {}
    # Source of truth for "the partner" is the conversation cast, not the
    # stale metadata.random_picks. The user can swap via the staging
    # dropdown (which updates picks) OR via the right-side Library +/-
    # (which doesn't). When they go through the Library path, picks
    # would still reference the original roll — and if that character's
    # been removed from cast, partner_name resolves to None and the
    # `{{partner_name}}` macro shows up literally in the prose. Fix it
    # by walking the actual cast and picking whoever's in the
    # `random_character_pool`. If the metadata-recorded partner is still
    # in cast we honor it (preserves the dropdown's selection); if not,
    # fall back to whatever pool member IS in cast.
    pool_chars = list((scenario.get("random_character_pool") or []))
    in_cast_pool = [
        eid for eid, e in instance_ents.items()
        if isinstance(e, dict) and e.get("type") == "character"
        and eid != "user" and eid in pool_chars
    ]
    meta_partner = picks.get("partner")
    if meta_partner and meta_partner in in_cast_pool:
        partner_id = meta_partner
    elif in_cast_pool:
        partner_id = in_cast_pool[0]
    else:
        partner_id = meta_partner  # last resort; may not resolve a name

    # When the cast-derived partner differs from what the staging
    # roots' metadata says, propagate the new partner across EVERY
    # staging root so the [Scenario] block, opening prompt, and
    # sidebar reads consistent text regardless of which setup the
    # user navigates to.
    if partner_id and partner_id != meta_partner:
        new_partner_name = (instance_ents.get(partner_id) or {}).get("name") or partner_id
        propagate_partner_to_staging_roots(conv, scenario, partner_id, new_partner_name)

    partner_name = None
    if partner_id and partner_id in instance_ents:
        partner_name = (instance_ents[partner_id].get("name") or partner_id)
    item_id = picks.get("magic_item")
    item_name = None
    if item_id and item_id in instance_ents:
        item_name = (instance_ents[item_id].get("name") or item_id)

    def _sub(s: str) -> str:
        return substitute_macros(
            s,
            partner_id=partner_id,
            partner_name=partner_name,
            magic_item_id=item_id,
            magic_item_name=item_name,
        )

    # Build the narrator child message.
    now = int(time.time())
    nid = f"msg_{uuid.uuid4().hex[:10]}"
    opening_text = _sub(setup.get("opening_prompt") or "").strip()
    snap = dict(target_root.get("presence_snapshot") or {})
    conv["messages"][nid] = {
        "id": nid,
        "parent_id": target_root["id"],
        "persona": "narrator",
        "speaker_id": None,
        "content": opening_text,
        "presence_snapshot": snap,
        "created_at": now,
        "edited_at": None,
        "metadata": {"staging_started": True},
    }

    # First-message greetings hang off the narrator child. We sub
    # macros in fm keys + values so any "{{partner}}: ..." entries
    # resolve to the current partner.
    fm_resolved = {
        _sub(k): _sub(v)
        for k, v in (setup.get("first_messages") or {}).items()
        if isinstance(k, str) and isinstance(v, str)
    }
    setup_for_fm = dict(setup)
    setup_for_fm["first_messages"] = fm_resolved
    leaf = _seed_first_message_chain(
        conv, scenario, instance_ents, nid, snap, setup_for_fm, is_reseed=False,
    )
    conv["active_path_leaf"] = leaf or nid
    record_branch_choice_path(conv, conv["active_path_leaf"])
    save_conversation(conv)
    return conv


_VALID_SLOT_NAMES = {
    "top", "bottom", "bra", "panties", "pantyhose", "gloves", "legwear", "shoes",
}


def start_scene_staging(
    conversation_id: str,
    *,
    setup_id: str,
    picks: dict[str, Any],
    extra_edits: list[dict[str, Any]] | None = None,
    body_override: str | None = None,
    narrator_directive: str | None = None,
    narrator_raw_response: str | None = None,
    location_directive: str | None = None,
    location_raw_response: str | None = None,
    scenario_instructions_base: str | None = None,
    scenario_instructions_append: str | None = None,
    modules: list[str] | None = None,
    module_settings: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Spawn a new SIBLING ROOT off the conversation tree from a Scene
    staging root's picks. The staging root itself stays untouched —
    the panel is permanent — and each Start adds another sibling root
    next to it, navigable via the existing setup-picker branch chips.

    No first_messages are seeded; the new root is just narrator
    opening + presence_snapshot, and the conversation awaits the next
    turn (NPC starts is handled frontend-side via sessionStorage +
    streamGenerate, same mechanism trap uses).

    `extra_edits` / `body_override` carry the model-generated narrator
    edits + rewritten body when the route layer ran the narrator
    against the user's free-text directive. They land on the new root
    alongside the dropdown picks so path-replay sees one combined
    edit log on a single sibling root.

    `picks` shape:
        characters:    list[str]            — picked character ids (>= 1)
        outfits:       dict[str, str]       — char_id → outfit_id
        slot_states:   dict[str, dict[str,int]]
                                            — char_id → {slot: 1|2|3}
                                              (only non-default slots
                                               are passed through; 1=on,
                                               2=partial, 3=off)
        location:      str                  — location id
        room:          str                  — room id
        prompt:        str                  — narrator opening prose
                                              (overridden by body_override
                                              when narrator runs)
    """
    conv = load_conversation(conversation_id)
    if not conv:
        raise ValueError(f"Conversation {conversation_id!r} not found.")

    chars = [
        c for c in (picks.get("characters") or [])
        if isinstance(c, str) and c
    ]
    if not chars:
        raise ValueError("Pick at least one character.")
    location = picks.get("location") or ""
    room = picks.get("room") or ""
    if not isinstance(location, str) or not location:
        raise ValueError("Location is required.")
    if not isinstance(room, str) or not room:
        raise ValueError("Room is required.")
    outfits = picks.get("outfits") or {}
    if not isinstance(outfits, dict):
        outfits = {}
    # Optional per-character placement overrides (char_id -> location/room id).
    # The top-level location/room remain the scene default used for any
    # character without an override, so this is fully back-compatible with the
    # old batch-only behaviour.
    cast_locations = picks.get("cast_locations") or {}
    if not isinstance(cast_locations, dict):
        cast_locations = {}
    cast_rooms = picks.get("cast_rooms") or {}
    if not isinstance(cast_rooms, dict):
        cast_rooms = {}
    slot_states = picks.get("slot_states") or {}
    if not isinstance(slot_states, dict):
        slot_states = {}
    prompt = picks.get("prompt")
    if not isinstance(prompt, str):
        prompt = ""
    # Optional user_persona picked from a staging preset (or typed
    # custom). Emitted below as [set user.name = …] / [set
    # user.description = …] edits on the new root so the branch's
    # effective persona path-replays correctly. None means "leave
    # whatever the parent setup root carried."
    # The pick can carry either {name, description} (legacy: replace
    # the user's identity) or {role, role_description} (role overlay:
    # keep the user's identity, add a role-in-this-scene). Both pass
    # through as-is into the edit emission below; deep_merge stamps
    # each key onto user.* via _nest_value.
    user_persona_pick = picks.get("user_persona")
    if isinstance(user_persona_pick, dict):
        cleaned: dict[str, str] = {}
        for k in ("name", "description", "role", "role_description"):
            v = user_persona_pick.get(k)
            if isinstance(v, str) and v.strip():
                cleaned[k] = v.strip()
            elif isinstance(v, str):
                # Empty-string for the description fields is allowed
                # (clears any prior value).
                cleaned[k] = ""
        # No usable identity or role payload → drop.
        if not (cleaned.get("name") or cleaned.get("role")):
            user_persona_pick = None
        else:
            user_persona_pick = cleaned
    else:
        user_persona_pick = None

    # Find the Scene staging root so we can read the setup name +
    # confirm the setup_id is actually a Scene staging setup. The
    # staging root itself isn't modified — we just spawn a sibling.
    target_root = None
    for m in conv.get("messages", {}).values():
        meta = m.get("metadata") or {}
        if not meta.get("scene_staging") or m.get("parent_id") is not None:
            continue
        if (meta.get("setup") or {}).get("id") == setup_id:
            target_root = m
            break
    if not target_root:
        raise ValueError(f"No Scene staging root for setup {setup_id!r}.")

    # Pull picked characters into the instance dir if missing —
    # covers scenarios whose Scene staging pool extends beyond
    # scenario.characters. File-management only; cast membership is
    # established below as branch-scoped cast_add / cast_remove
    # edits on the new staging root, NOT mutated onto the shared
    # instance scenario.
    from . import layers, entities as ent
    for cid in chars:
        if ent.load_instance_entity(conversation_id, cid) is not None:
            continue
        try:
            layers.instance_entity_into_conversation(conversation_id, cid)
        except Exception:
            pass
    instance_ents = ent.load_instance_entities(conversation_id) or {}

    # Cast on the new branch = exactly the user's picks (plus user).
    # Emit cast_remove for every scenario character NOT picked, then
    # cast_add for every pick. Path-replay starts from the shared
    # scenario baseline (instance scenario .characters[]), so this
    # produces a branch with just the picked cast — exactly what the
    # user expects from "If I choose Iris, the branch has Iris."
    scenario_for_cast = ent.load_instance_entity(
        conversation_id, conv.get("scenario_id") or ""
    ) or {}
    scenario_chars = list(scenario_for_cast.get("characters") or [])
    picks_set = set(chars)
    edits: list[dict[str, Any]] = []
    # Persona picks land first so _user_persona_from_edits sees them
    # when seeding the new root's setup metadata. Emitted as plain
    # [set user.<field> = <value>] patches against the user instance
    # entity — path-replay (effective_user_persona) reads them back
    # on every render of this branch.
    if user_persona_pick:
        # Each key the cleaner kept lands as its own patch on user.<k>.
        # In role-mode scenarios that's role + role_description; in
        # legacy mode it's name + description. Either way the keys
        # the cleaner included reflect the user's pick from the panel.
        for _k in ("name", "description", "role", "role_description"):
            if _k in user_persona_pick:
                edits.append({
                    "kind": "patch",
                    "id": "user",
                    "data": {_k: user_persona_pick[_k]},
                })
    for sc in scenario_chars:
        if sc == "user" or sc in picks_set:
            continue
        edits.append({"kind": "cast_remove", "id": sc})
    for cid in chars:
        if cid != "user":
            edits.append({"kind": "cast_add", "id": cid})
    # Optional user-outfit pick from the staging panel. The panel
    # surfaces a user-outfit row (sourced from outfits_for the user
    # template); we emit a single [outfit user -> X] edit so the
    # user's wardrobe state is staged the same way cast outfits are.
    # Done before the per-character emission so the user-outfit edit
    # sits cleanly at the top of the cast outfit changes.
    user_outfit_id = outfits.get("user")
    if isinstance(user_outfit_id, str) and user_outfit_id:
        edits.append({
            "kind": "outfit",
            "character_id": "user",
            "outfit_id": user_outfit_id,
        })
    for cid in chars:
        cloc = cast_locations.get(cid) or location
        crm = cast_rooms.get(cid) or room
        edits.append({
            "kind": "move",
            "character_id": cid,
            "location": cloc if isinstance(cloc, str) and cloc else location,
            "room": crm if isinstance(crm, str) and crm else room,
        })
        outfit_id = outfits.get(cid)
        if isinstance(outfit_id, str) and outfit_id:
            edits.append({
                "kind": "outfit",
                "character_id": cid,
                "outfit_id": outfit_id,
            })
        # Slot overrides land on the character via clothing_overrides
        # (auto_state.py:181 already reads this field on top of the
        # outfit's clothing_slots), so a single [set] per slot is the
        # right shape — that's the same path the auto-state side call
        # produces for the renderer.
        char_slots = slot_states.get(cid) or {}
        if not isinstance(char_slots, dict):
            continue
        for slot, value in char_slots.items():
            if not isinstance(slot, str) or slot.lower() not in _VALID_SLOT_NAMES:
                continue
            try:
                v = int(value)
            except (TypeError, ValueError):
                continue
            if v not in (1, 2, 3):
                continue
            edits.append({
                "kind": "patch",
                "id": cid,
                "data": _nest_value(
                    f"properties.clothing_overrides.{slot.lower()}", v,
                ),
            })

    # Accessories (mix-and-match) — per-character list of accessory
    # outfit ids that compose on top of the primary outfit.  Emitted as
    # `[set <char>.properties.accessories = [...]]` patches; the
    # accessory composer in personas._compose_accessories reads the
    # list at render time. Each accessory falls back to the global
    # template catalog so it doesn't need to be conversation-instanced
    # ahead of time.
    accessories = picks.get("accessories") or {}
    if not isinstance(accessories, dict):
        accessories = {}
    for cid in chars:
        if cid == "user":
            continue
        acc_ids = accessories.get(cid)
        if not isinstance(acc_ids, list):
            continue
        cleaned = [a for a in acc_ids if isinstance(a, str) and a]
        if not cleaned:
            continue
        edits.append({
            "kind": "patch", "id": cid,
            "data": {"properties": {"accessories": cleaned}},
        })

    # Outfit overrides (per-character {color}/{material}/{fit}/{style}
    # overlay). Emitted as `[set <char>.properties.outfit_overrides.K
    # = V]` patches. The outfit-templating layer
    # (personas._apply_outfit_template) reads these to substitute
    # placeholders in the primary outfit's prose.
    outfit_overrides = picks.get("outfit_overrides") or {}
    if not isinstance(outfit_overrides, dict):
        outfit_overrides = {}
    for cid in chars:
        if cid == "user":
            continue
        char_overrides = outfit_overrides.get(cid)
        if not isinstance(char_overrides, dict):
            continue
        for key in ("color", "material", "fit", "style"):
            val = char_overrides.get(key)
            if not isinstance(val, str) or not val.strip():
                continue
            edits.append({
                "kind": "patch", "id": cid,
                "data": _nest_value(
                    f"properties.outfit_overrides.{key}", val.strip(),
                ),
            })

    # Per-instance overrides for location / room descriptions. The
    # staging panel surfaces the current loc/room description in an
    # editable textarea and sends only entries that diverge from the
    # template; each lands as a [patch] against the location / room
    # entity's `description` field so path-replay (and personas.py
    # _surroundings_text) picks up the override at prompt build.
    for picks_key in ("location_descriptions", "room_descriptions"):
        raw_map = picks.get(picks_key) or {}
        if not isinstance(raw_map, dict):
            continue
        for ent_id, new_desc in raw_map.items():
            if not isinstance(ent_id, str) or not ent_id:
                continue
            if not isinstance(new_desc, str):
                continue
            edits.append({
                "kind": "patch",
                "id": ent_id,
                "data": {"description": new_desc},
            })

    # Narrator-generated edits ride on the same applied_edits log as
    # the dropdown picks so path-replay sees one coherent edit list
    # on a single sibling root.
    if extra_edits:
        edits.extend(e for e in extra_edits if isinstance(e, dict))

    setup_meta = (target_root.get("metadata") or {}).get("setup") or {}
    setup_name = setup_meta.get("name") or "Scene staging"

    # Scenario instructions for the new root: edited base + append.
    # Both are user-typed in the panel; if the user didn't touch the
    # field, falls back to the staging root's resolved values so a
    # blank submit doesn't wipe inherited instructions.
    if isinstance(scenario_instructions_base, str):
        base_inst = scenario_instructions_base.strip()
    else:
        base_inst = (setup_meta.get("scenario_instructions_base") or "").strip()
    if isinstance(scenario_instructions_append, str):
        append_inst = scenario_instructions_append.strip()
    else:
        append_inst = ""
    if base_inst and append_inst:
        instructions = base_inst + "\n\n" + append_inst
    else:
        instructions = append_inst or base_inst

    # Body comes from the narrator's rewritten output when one ran;
    # otherwise the user's typed prompt is the opening prose.
    final_body = body_override if (
        isinstance(body_override, str) and body_override.strip()
    ) else prompt

    # seed_setup_root_from_directive baselines presence off the active
    # leaf; we want the staging root's snapshot (which is the scenario
    # baseline) to be the starting point so a fresh stage doesn't
    # inherit some unrelated branch's edits. Pin active_path_leaf to
    # the staging root for the duration of the call, then we re-pin
    # to the new root afterwards.
    saved_leaf = conv.get("active_path_leaf")
    conv["active_path_leaf"] = target_root["id"]
    directive_text = (narrator_directive or "").strip()
    try:
        new_root = seed_setup_root_from_directive(
            conv,
            name=f"{setup_name} — staged",
            description="",
            opening_prompt=final_body,
            instructions=instructions,
            directive=directive_text,
            edits=edits,
        )
    except Exception:
        conv["active_path_leaf"] = saved_leaf
        raise

    # Tag the new root so the chat UI can render the "Applied N edits"
    # confirmation banner below it after page reload.
    new_meta = new_root.setdefault("metadata", {})
    new_meta["scene_staging_origin"] = True
    new_meta["scene_staging_source_setup_id"] = setup_id
    # Picked-cast list lives on the root so the prompt assembler can
    # filter the cast block down to the chars the user actually
    # picked (instead of every scenario character).
    new_meta["scene_staging_picks"] = {"characters": list(chars)}
    # Modules + per-module settings the user picked on the staging
    # panel. Already validated against the scenario's available_modules
    # by the route layer; we just stamp the validated list and the
    # default-merged settings onto the new root so path-replay carries
    # them with the rest of the branch state.
    new_meta["modules"] = list(modules or [])
    new_meta["module_settings"] = dict(module_settings or {})
    # Mirror the resolved instructions onto setup metadata so the
    # path-replay reader (effective_scenario_instructions) and the
    # left-panel display both pick them up.
    setup_block = new_meta.setdefault("setup", {})
    setup_block["scenario_instructions"] = instructions
    setup_block["scenario_instructions_base"] = base_inst
    setup_block["scenario_instructions_append"] = append_inst
    if directive_text or location_directive:
        new_meta["narrator_edit"] = {
            "directive": directive_text,
            "raw_response": narrator_raw_response or "",
            "edits": list(extra_edits or []),
            "location_directive": (location_directive or ""),
            "location_raw_response": (location_raw_response or ""),
        }

    conv["active_path_leaf"] = new_root["id"]
    record_branch_choice_path(conv, conv["active_path_leaf"])
    save_conversation(conv)
    return conv


def _seed_grid_message(
    conversation: dict[str, Any],
    parent_id: str,
    snap: dict[str, Any],
    grid_message: dict[str, Any],
    is_reseed: bool,
) -> str:
    """Append a Grid Message node under `parent_id`. It carries the tactical-grid
    seed in ``metadata.pf1e_grid_message`` (the pf1e module reads it to seed the
    field) and a short narrator line so the transcript shows the hand-off. Returns
    the new node id (the active leaf), so the conversation opens on the grid."""
    now = int(time.time())
    gid = f"msg_{uuid.uuid4().hex[:10]}"
    intro = str(grid_message.get("intro") or "").strip() or "⚔ The scene moves to the tactical grid."
    meta: dict[str, Any] = {"pf1e_grid_message": grid_message}
    if is_reseed:
        meta["reseeded"] = True
    conversation["messages"][gid] = {
        "id": gid,
        "parent_id": parent_id,
        "persona": "narrator",
        "speaker_id": None,
        "content": intro,
        "presence_snapshot": snap,
        "created_at": now,
        "edited_at": None,
        "metadata": meta,
    }
    return gid


def _seed_first_message_chain(
    conversation: dict[str, Any],
    scenario: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    root_id: str,
    snap: dict[str, Any],
    setup: dict[str, Any],
    is_reseed: bool,
) -> str:
    """Hang the per-character first_message greetings off `root_id`.
    Returns the id of the last message in the chain (or root_id if none)."""
    now = int(time.time())
    parent_id = root_id
    fm = setup.get("first_messages") or {}
    for char_id in scenario.get("characters", []) or []:
        char = entities.get(char_id)
        if not char:
            continue
        override = (fm.get(char_id) or "").strip()
        first_msg = override or ((char.get("properties") or {}).get("first_message") or "").strip()
        if not first_msg:
            continue
        # Apply only the deterministic one-shot macros at creation; keep
        # {{user}} / {{char}} as templates so changing the user persona
        # later updates the rendered greeting.
        first_msg = apply_macros(first_msg, {
            "user_name": "{{user}}",
            "char_name": "{{char}}",
        })
        gid = f"msg_{uuid.uuid4().hex[:10]}"
        meta = {"first_message": True}
        if is_reseed:
            meta["reseeded"] = True
        conversation["messages"][gid] = {
            "id": gid,
            "parent_id": parent_id,
            "persona": char_id,
            "speaker_id": char_id,
            "content": first_msg,
            "presence_snapshot": snap,
            "created_at": now,
            "edited_at": None,
            "metadata": meta,
        }
        parent_id = gid
    return parent_id


def _nest_value(key: str, value: Any) -> dict[str, Any]:
    """Wrap `value` so it's reachable at `key` when deep-merged.

    `key` may be dotted (e.g. "properties.outfits") for nested writes.
    """
    parts = str(key).split(".")
    out: Any = value
    for p in reversed(parts):
        out = {p: out}
    return out


def seed_setup_root_from_directive(
    conversation: dict[str, Any],
    *,
    name: str,
    description: str,
    opening_prompt: str,
    instructions: str,
    directive: str,
    edits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Add a new sibling root to `conversation` representing a setup
    built from a free-text directive. Returns the new root message dict.

    The provided `edits` are run through `apply_edits` so the
    applied_log shape matches what path-replay expects, and the new
    root's presence_snapshot reflects move/outfit deltas. Entity-state
    edits never touch the instance file (path-replay handles them at
    render time).
    """
    cid = conversation["id"]
    msgs = conversation.get("messages") or {}
    # Pick a baseline snapshot to layer the new setup's presence on.
    # Active leaf's snapshot is the natural choice — the user is
    # branching off from "here." Fall back to any existing root's
    # snapshot if the active leaf has no presence (rare).
    baseline = {}
    leaf_id = conversation.get("active_path_leaf") or ""
    leaf = msgs.get(leaf_id)
    if leaf and isinstance(leaf.get("presence_snapshot"), dict):
        baseline = {
            "presence": dict((leaf["presence_snapshot"].get("presence") or {})),
            "objects_present": dict((leaf["presence_snapshot"].get("objects_present") or {})),
        }
    if not baseline.get("presence"):
        for r in msgs.values():
            if not r.get("parent_id") and isinstance(r.get("presence_snapshot"), dict):
                baseline = {
                    "presence": dict((r["presence_snapshot"].get("presence") or {})),
                    "objects_present": dict((r["presence_snapshot"].get("objects_present") or {})),
                }
                break

    # Brand-new setup root → its "in-cast on entry" is the scenario
    # baseline (characters[] + user). Pass it so apply_edits drops the
    # redundant paired cast_adds the staging emit pile generates (move
    # / outfit / patch for each picked char each emit a paired cast_add
    # via narrator_apply._record_edit since the 1f3ee21 fix; the
    # baseline already has these characters, so the cast_add would just
    # be log noise). cast_remove for non-picked chars still fires and
    # discards them from the running set; cast_add for genuinely-new
    # ids (off-cast picks that aren't in the baseline) still fires.
    from .effective import effective_cast_at as _ec
    existing_cast = _ec(conversation, None).get("characters") or set()
    presence_patch, applied_log = _apply_narrator_edits(
        cid,
        edits,
        baseline,
        existing_cast_chars=existing_cast,
    )
    snap = _merge_presence_patch(baseline, presence_patch)
    # Prune presence for characters this root's cast_remove edits cut, so
    # they don't leave a phantom placement row that raw-presence readers
    # (map panel, follower sweep) would still see. The prompt is already
    # clean via branch_filter; this keeps the snapshot data honest too.
    snap = _prune_presence_to_cast(snap, _cast_after_log(set(existing_cast), applied_log))

    setup_id = _unique_setup_id(conversation, name)
    user_persona = _user_persona_from_edits(edits)

    setup_meta = {
        "id": setup_id,
        "name": (name or setup_id).strip()[:60] or setup_id,
        "description": (description or "").strip(),
        "scenario_instructions": _resolved_instructions(conversation, instructions),
        "user_persona": user_persona,
        "state": _edits_to_state_text(edits),
        "from_directive": directive.strip(),
    }

    root_id = f"msg_{uuid.uuid4().hex[:10]}"
    root_message = {
        "id": root_id,
        "parent_id": None,
        "persona": "narrator",
        "speaker_id": None,
        "content": opening_prompt or "",
        "presence_snapshot": snap,
        "created_at": int(time.time()),
        "edited_at": None,
        "metadata": {
            "opening": True,
            "setup": setup_meta,
            "applied_edits": applied_log,
        },
    }
    conversation["messages"][root_id] = root_message
    return root_message


def _unique_setup_id(conversation: dict[str, Any], name: str) -> str:
    """Build a snake_case id from `name`, deduped against existing setup
    roots in the conversation."""
    import re as _re
    base = _re.sub(r"[^a-z0-9]+", "_", (name or "directive").lower()).strip("_")
    if not base:
        base = "directive"
    used: set[str] = set()
    for m in (conversation.get("messages") or {}).values():
        s = (m.get("metadata") or {}).get("setup")
        if isinstance(s, dict) and s.get("id"):
            used.add(s["id"])
    if base not in used:
        return base
    n = 2
    while f"{base}_{n}" in used:
        n += 1
    return f"{base}_{n}"


def _user_persona_from_edits(edits: list[dict[str, Any]]) -> dict[str, Any]:
    """Pull a snapshot of user-targeting edits into a flat persona dict
    so the chat UI / settings mirror have a name/description/etc to
    display when this setup is active. Path-replay still owns the
    authoritative value at render time."""
    persona: dict[str, Any] = {"name": "User", "description": ""}
    for e in edits:
        if (e.get("kind") in ("patch", "set")) and e.get("id") == "user":
            data = e.get("data") or {}
            if isinstance(data, dict):
                _flatten_into(persona, data)
    return persona


def _flatten_into(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _flatten_into(dst[k], v)
        else:
            dst[k] = v


def _resolved_instructions(conversation: dict[str, Any], append: str) -> str:
    """scenario_instructions for the new setup = scenario base +
    `append`. Settings.scenario_instructions holds the active setup's
    resolved value, but for the BASE we fall back to the conversation's
    earliest setup root if available, then to settings."""
    msgs = conversation.get("messages") or {}
    base = ""
    settings = conversation.get("settings") or {}
    base = (settings.get("scenario_instructions") or "").strip()
    # If any existing setup root carries a base scenario_instructions,
    # prefer that — it's the canonical source if the user has been
    # editing the active setup's instructions in place.
    for m in msgs.values():
        if not m.get("parent_id"):
            s = (m.get("metadata") or {}).get("setup")
            if isinstance(s, dict) and isinstance(s.get("scenario_instructions"), str):
                base = s["scenario_instructions"]
                break
    append = (append or "").strip()
    if base and append:
        # Avoid double-appending if the existing base already ended with
        # the append text (idempotency).
        if base.endswith(append):
            return base
        return base + "\n\n" + append
    return append or base


def _edits_to_state_text(edits: list[dict[str, Any]]) -> str:
    """Reverse-render an edit list into the directive grammar so the
    setup's `state` field (used by the studio editor) reflects what
    actually happened. Best-effort — patches with non-trivial data may
    not round-trip cleanly."""
    lines: list[str] = []
    for e in edits:
        kind = e.get("kind")
        if kind == "move":
            cid = e.get("character_id"); room = e.get("room"); loc = e.get("location")
            if cid and room:
                target = f"{loc}:{room}" if loc else room
                lines.append(f"[move {cid} -> {target}]")
        elif kind == "outfit":
            cid = e.get("character_id"); oid = e.get("outfit_id")
            if cid and oid:
                lines.append(f"[outfit {cid} -> {oid}]")
        elif kind in ("patch", "set"):
            eid = e.get("id"); data = e.get("data") or {}
            for path, val in _flat_patch_paths(data):
                lines.append(f"[set {eid}.{path} = {_format_value(val)}]")
        elif kind == "unset":
            eid = e.get("id"); path = e.get("path") or []
            if eid and path:
                lines.append(f"[unset {eid}.{'.'.join(path)}]")
    return "\n".join(lines)


def _flat_patch_paths(data: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    """Yield (dotted-path, value) pairs from a nested dict."""
    out: list[tuple[str, Any]] = []
    for k, v in data.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.extend(_flat_patch_paths(v, path))
        else:
            out.append((path, v))
    return out


def _format_value(v: Any) -> str:
    import json as _json
    if isinstance(v, str):
        return _json.dumps(v)
    return _json.dumps(v)


def _baseline_presence_from_scenario(
    scenario: dict[str, Any], entities: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    raw = scenario.get("starting_state") or {}
    presence: dict[str, dict[str, Any]] = {}
    for char_id in scenario.get("characters") or []:
        defaults = raw.get(char_id) or {}
        presence[char_id] = {
            "location": defaults.get("location"),
            "room": defaults.get("room"),
            "outfit": defaults.get("outfit")
                or (entities.get(char_id, {}).get("properties") or {}).get("current_outfit"),
        }
    return {
        "presence": presence,
        "objects_present": dict(raw.get("objects_present", {}) or {}),
    }


def _merge_presence_patch(
    baseline: dict[str, Any], patch_by_char: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Layer apply_edits' presence_patch onto the baseline snapshot."""
    out = {
        "presence": dict(baseline.get("presence") or {}),
        "objects_present": dict(baseline.get("objects_present") or {}),
    }
    for char_id, patch in patch_by_char.items():
        prev = dict(out["presence"].get(char_id) or {})
        prev.update({k: v for k, v in patch.items() if v})
        out["presence"][char_id] = prev
    return out


def _cast_after_log(existing_cast: set[str], applied_log: list[dict[str, Any]]) -> set[str]:
    """Replay a just-built applied_log's cast_add/cast_remove onto the
    baseline cast set, returning the resulting character-cast set. `user`
    is always kept. Used to know who is actually on the new root before
    it's committed (so effective_cast_at, which reads committed messages,
    can't be consulted yet)."""
    cast = set(existing_cast) | {"user"}
    for e in applied_log or []:
        kind, eid = e.get("kind"), e.get("id")
        if not eid:
            continue
        if kind == "cast_add":
            cast.add(eid)
        elif kind == "cast_remove" and eid != "user":
            cast.discard(eid)
    return cast


def _prune_presence_to_cast(snap: dict[str, Any], cast_ids: set[str]) -> dict[str, Any]:
    """Drop presence rows for characters not in `cast_ids` (keeping
    `user`). This is the staging/narrator analogue of what
    `layers.remove_from_conversation_cast` already does on the side-panel
    remove: a cast_remove must also pop the character's placement, or the
    cut character lingers as a phantom row in the snapshot — invisible to
    the (branch-filtered) prompt, but still read by raw-presence
    consumers (the map panel, the move-time follower sweep). Objects are
    left alone: they use a separate opt-in membership model and aren't
    part of the scenario character pool that staging cuts.
    """
    keep = set(cast_ids) | {"user"}
    presence = snap.get("presence")
    if isinstance(presence, dict):
        snap["presence"] = {k: v for k, v in presence.items() if k in keep}
    return snap


# ---------------------------------------------------------------------------
# Tree ops
# ---------------------------------------------------------------------------


def path_to_root(conversation: dict[str, Any], leaf_id: str) -> list[dict[str, Any]]:
    """Return messages from root to leaf along the active path."""
    messages = conversation["messages"]
    chain: list[dict[str, Any]] = []
    cur = messages.get(leaf_id)
    seen: set[str] = set()
    while cur and cur["id"] not in seen:
        chain.append(cur)
        seen.add(cur["id"])
        if cur["parent_id"] is None:
            break
        cur = messages.get(cur["parent_id"])
    chain.reverse()
    return chain


def active_path(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    return path_to_root(conversation, conversation["active_path_leaf"])


def children_of(conversation: dict[str, Any], parent_id: str | None) -> list[dict[str, Any]]:
    # Ordered oldest-first by creation time so sibling/branch numbering is
    # stable and chronological (Branch 1 = oldest). `created_at` is only
    # second-resolution, so tie-break on id for determinism. Without this the
    # result followed dict insertion order, which drifts after reloads/regens.
    kids = [
        m for m in conversation["messages"].values() if m["parent_id"] == parent_id
    ]
    kids.sort(key=lambda m: (m.get("created_at", 0), m.get("id", "")))
    return kids


def descendant_ids(conversation: dict[str, Any], message_id: str) -> set[str]:
    """All descendants (not including the node itself)."""
    out: set[str] = set()
    stack = [message_id]
    while stack:
        cur = stack.pop()
        for child in children_of(conversation, cur):
            if child["id"] not in out:
                out.add(child["id"])
                stack.append(child["id"])
    return out


def append_message(
    conversation: dict[str, Any],
    *,
    parent_id: str | None,
    persona: str,
    content: str,
    speaker_id: str | None = None,
    presence_snapshot: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a new message under `parent_id`. Pass parent_id=None (or an
    empty string) to add a new root — useful after deleting the root and
    starting fresh."""
    parent: dict[str, Any] | None = None
    if parent_id:
        if parent_id not in conversation["messages"]:
            raise ValueError(f"parent_id {parent_id!r} not in conversation.")
        parent = conversation["messages"][parent_id]
    msg_id = f"msg_{uuid.uuid4().hex[:10]}"
    snapshot = presence_snapshot or (parent.get("presence_snapshot") if parent else {}) or {}
    msg = {
        "id": msg_id,
        "parent_id": parent_id or None,
        "persona": persona,
        "speaker_id": speaker_id,
        "content": content,
        "presence_snapshot": snapshot,
        "created_at": int(time.time()),
        "edited_at": None,
        "metadata": metadata or {},
    }
    conversation["messages"][msg_id] = msg
    conversation["active_path_leaf"] = msg_id
    record_branch_choice_path(conversation, msg_id)
    return msg


def edit_message_in_place(
    conversation: dict[str, Any],
    message_id: str,
    new_content: str,
) -> dict[str, Any]:
    """Mutate the existing message's content directly. No branch is
    created; descendants stay attached. Used by the "Raw" edit action
    when the user wants to fix a typo without forking history."""
    msg = conversation["messages"].get(message_id)
    if not msg:
        raise ValueError(f"Message {message_id!r} not found.")
    msg["content"] = new_content
    msg["edited_at"] = int(time.time())
    return msg


def edit_message_as_branch(
    conversation: dict[str, Any],
    message_id: str,
    new_content: str,
) -> dict[str, Any]:
    """Editing always creates a new sibling branch instead of mutating in place.

    For root messages this means appending a new root (parent_id=None);
    sibling navigation already supports multiple roots, so the user can
    flip between the original opening and the edited one.
    """
    original = conversation["messages"].get(message_id)
    if not original:
        raise ValueError(f"Message {message_id!r} not found.")
    return append_message(
        conversation,
        parent_id=original["parent_id"],
        persona=original["persona"],
        content=new_content,
        speaker_id=original.get("speaker_id"),
        presence_snapshot=original.get("presence_snapshot"),
        metadata={**(original.get("metadata") or {}), "edited_from": message_id},
    )


def delete_subtree(conversation: dict[str, Any], message_id: str) -> int:
    """Delete a message and every descendant. Returns the number removed.

    Root deletion is allowed (everything is per-conversation, so an empty
    conversation is fine — the user can compose freely and rebuild). If the
    root has multiple children, they all become roots; the first one (or
    whichever path the active leaf was on) is promoted as the new active leaf.
    """
    if message_id not in conversation["messages"]:
        return 0
    parent_id = conversation["messages"][message_id]["parent_id"]
    to_remove = descendant_ids(conversation, message_id) | {message_id}
    for mid in to_remove:
        conversation["messages"].pop(mid, None)
    if conversation["active_path_leaf"] in to_remove:
        if parent_id and parent_id in conversation["messages"]:
            conversation["active_path_leaf"] = parent_id
        else:
            # Root was deleted (or its parent was also removed). Pick any
            # remaining message as the new leaf, preferring a leaf-like
            # node. If nothing is left, leave it pointing nowhere — the
            # composer still works against an empty tree.
            remaining = list(conversation["messages"].keys())
            conversation["active_path_leaf"] = remaining[-1] if remaining else ""
    # Re-parent any orphaned messages whose parent_id was removed.
    for m in conversation["messages"].values():
        if m.get("parent_id") and m["parent_id"] not in conversation["messages"]:
            m["parent_id"] = None
    prune_branch_choices(conversation)
    return len(to_remove)


def set_active_leaf(conversation: dict[str, Any], leaf_id: str) -> None:
    if leaf_id not in conversation["messages"]:
        raise ValueError(f"Message {leaf_id!r} not found.")
    conversation["active_path_leaf"] = leaf_id
    record_branch_choice_path(conversation, leaf_id)
    # Path-based effective state: switching root just changes which
    # setup's edits land in the replayed path. We still mirror the
    # active setup's instructions / user persona into settings as a
    # convenience for code that reads settings directly (studio,
    # exports). Render-time always uses effective_* helpers, so this
    # is best-effort, not authoritative.
    new_root = _root_of_path(conversation, leaf_id)
    if new_root:
        _mark_active_setup_root(conversation, new_root)


def _root_of_path(conversation: dict[str, Any], leaf_id: str) -> str | None:
    """Walk a leaf back to its root; return the root id or None."""
    msgs = conversation.get("messages") or {}
    cur = msgs.get(leaf_id)
    seen: set[str] = set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        if not cur.get("parent_id"):
            return cur["id"]
        cur = msgs.get(cur["parent_id"])
    return None


def _mark_active_setup_root(conversation: dict[str, Any], root_id: str) -> None:
    """Flip the `setup_active` flag on root metadata + mirror the
    setup's resolved instructions / user persona into settings."""
    msgs = conversation.get("messages") or {}
    root = msgs.get(root_id) or {}
    meta = root.get("metadata") or {}
    setup = meta.get("setup")
    settings = conversation.setdefault("settings", {})
    if isinstance(setup, dict):
        if isinstance(setup.get("scenario_instructions"), str):
            settings["scenario_instructions"] = setup["scenario_instructions"]
        if isinstance(setup.get("user_persona"), dict):
            settings["user_persona"] = dict(setup["user_persona"])
    for r in msgs.values():
        if not r.get("parent_id"):
            r_meta = r.setdefault("metadata", {})
            if r["id"] == root_id:
                r_meta["setup_active"] = True
            else:
                r_meta.pop("setup_active", None)


def record_branch_choice_path(conversation: dict[str, Any], leaf_id: str) -> None:
    """Walk leaf → root and store {parent_id: child_on_path} for every step.

    Called whenever the active leaf changes so that switching to a sibling
    later can descend back to whichever leaf was last active inside that
    subtree.
    """
    msgs = conversation["messages"]
    choices = conversation.setdefault("branch_choices", {})
    cur_id = leaf_id
    seen: set[str] = set()
    while cur_id and cur_id not in seen:
        seen.add(cur_id)
        cur = msgs.get(cur_id)
        if not cur:
            break
        parent_id = cur.get("parent_id")
        if parent_id:
            choices[parent_id] = cur_id
        cur_id = parent_id


def prune_branch_choices(conversation: dict[str, Any]) -> None:
    """Drop entries that point at removed messages or stale parents."""
    msgs = conversation["messages"]
    choices = conversation.get("branch_choices") or {}
    conversation["branch_choices"] = {
        parent: child
        for parent, child in choices.items()
        if parent in msgs and child in msgs and msgs[child].get("parent_id") == parent
    }
