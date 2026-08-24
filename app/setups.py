"""Scenario setups: branchable pre-prompts.

A scenario can declare a list of `setups` — alternate "openings" that each
configure where characters are, what the user persona is, and the root
narrator message. They are seeded as sibling root messages on conversation
creation, so the user can navigate between them with the existing branch
arrows on the root.

Each setup carries:
  - id, name, description
  - opening_prompt          — the root narrator text
  - scenario_instructions_append  — extra rules appended to the scenario's
                                    base instructions for this setup only
  - first_messages          — per-character greeting overrides
  - user_persona            — overrides settings.user_persona
  - state                   — narrator-grammar text ([move], [outfit],
                              [set], [unset]) applied at creation

Inheritance: any field a setup omits falls back to the scenario root.
Scenarios without a `setups` list synthesize a single "default" setup
from the legacy fields so existing scenarios keep working unchanged.
"""
from __future__ import annotations

import copy
import random
import re
from typing import Any

from .narrator import extract_edits


DEFAULT_SETUP_ID = "default"


# ---------------------------------------------------------------------------
# Random pools + start toggles + macro substitution
#
# A scenario can declare:
#   - `random_character_pool`: list of character ids; one is rolled at
#                              conversation creation (or the user picks).
#   - `random_item_pool`:      list of object ids; one is rolled at
#                              creation if the items toggle is on.
#   - `start_toggles`:         list of {id, label, default} booleans the
#                              user picks at creation. Currently only
#                              `magic_item` (gates the item roll) is
#                              recognized by the seed code; other toggle
#                              ids are stored on the root for authors
#                              to reference from setup state via macros.
#
# Setup state, opening_prompt, scenario_instructions_append and
# first_messages keys/values can use the `{{partner}}` macro, which
# resolves to the character id picked from `random_character_pool` (or
# the first character in `characters[]` as a fallback). `{{partner_name}}`
# resolves to the character's display name.
# ---------------------------------------------------------------------------


_PARTNER_RE = re.compile(r"\{\{\s*partner\s*\}\}")
_PARTNER_NAME_RE = re.compile(r"\{\{\s*partner_name\s*\}\}")
_MAGIC_ITEM_RE = re.compile(r"\{\{\s*magic_item\s*\}\}")
_MAGIC_ITEM_NAME_RE = re.compile(r"\{\{\s*magic_item_name\s*\}\}")


def random_character_pool(scenario: dict[str, Any]) -> list[str]:
    raw = scenario.get("random_character_pool") or []
    return [x for x in raw if isinstance(x, str) and x]


def random_item_pool(scenario: dict[str, Any]) -> list[str]:
    raw = scenario.get("random_item_pool") or []
    return [x for x in raw if isinstance(x, str) and x]


def start_toggles(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    raw = scenario.get("start_toggles") or []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        tid = entry.get("id")
        if not isinstance(tid, str) or not tid:
            continue
        out.append({
            "id": tid,
            "label": entry.get("label") or tid,
            "default": bool(entry.get("default")),
        })
    return out


def roll_random_picks(
    scenario: dict[str, Any],
    *,
    overrides: dict[str, str] | None = None,
    toggles: dict[str, bool] | None = None,
    rng: random.Random | None = None,
) -> dict[str, str | None]:
    """Resolve the random picks for this conversation.

    Returns ``{"partner": <char_id>, "magic_item": <item_id|None>}``.
    Overrides win over a random roll, so a deterministic test or an
    explicit user pick can pin either field. The item roll is gated on
    the `magic_item` toggle (default false).
    """
    rng = rng or random.Random()
    overrides = overrides or {}
    toggles = toggles or {}

    chars = random_character_pool(scenario)
    if overrides.get("partner") and overrides["partner"] in chars:
        partner = overrides["partner"]
    elif chars:
        partner = rng.choice(chars)
    else:
        # No pool — fall back to the scenario's first character_id, or
        # leave unresolved (macros become passthrough).
        first = (scenario.get("characters") or [None])[0]
        partner = first if isinstance(first, str) else None

    item: str | None = None
    if toggles.get("magic_item"):
        items = random_item_pool(scenario)
        if overrides.get("magic_item") and overrides["magic_item"] in items:
            item = overrides["magic_item"]
        elif items:
            item = rng.choice(items)

    return {"partner": partner, "magic_item": item}


def substitute_macros(
    text: str,
    *,
    partner_id: str | None,
    partner_name: str | None = None,
    magic_item_id: str | None = None,
    magic_item_name: str | None = None,
) -> str:
    """Substitute the {{partner}} / {{partner_name}} / {{magic_item}} /
    {{magic_item_name}} tokens. Empty replacements when the macro has no
    resolved value (so the text reads cleanly even if items are off)."""
    if not text:
        return text
    out = text
    if partner_id is not None:
        out = _PARTNER_RE.sub(partner_id, out)
    if partner_name is not None:
        out = _PARTNER_NAME_RE.sub(partner_name, out)
    if magic_item_id is not None:
        out = _MAGIC_ITEM_RE.sub(magic_item_id, out)
    elif magic_item_id is None:
        out = _MAGIC_ITEM_RE.sub("", out)
    if magic_item_name is not None:
        out = _MAGIC_ITEM_NAME_RE.sub(magic_item_name, out)
    elif magic_item_name is None:
        out = _MAGIC_ITEM_NAME_RE.sub("", out)
    return out


def setup_list(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ordered list of resolved setups for a scenario.

    If the scenario has no `setups`, synthesize a single "default" setup
    from its root fields. Each returned setup is fully resolved (all
    fields filled in via inheritance from the scenario root).
    """
    raw = scenario.get("setups")
    if not isinstance(raw, list) or not raw:
        return [_resolve(scenario, _legacy_default_setup(scenario))]
    out: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict) and entry.get("id"):
            out.append(_resolve(scenario, entry))
    if not out:
        out.append(_resolve(scenario, _legacy_default_setup(scenario)))
    return out


def scene_staging_fields(setup: dict[str, Any]) -> dict[str, Any] | None:
    """Return a setup's `scene_staging_fields` dict if present, else None.

    The presence of this dict on a setup is the per-setup gate that
    turns it into a Scene staging root: the root is created with empty
    content + metadata flags so the chat UI hangs the Scene staging
    panel below it. The user picks characters / outfits / location /
    prompt on the panel; Start spawns a child branch with those picks.

    The shape is freeform — only the keys the panel knows how to render
    are honored. Today: `characters` (list[str]), `locations`
    (list[str]), optional `user_personas` (list[{id,name,description}])
    that drives the persona-preset dropdown on the panel. The prompt is
    a free-text textarea on the panel.
    """
    raw = setup.get("scene_staging_fields")
    if not isinstance(raw, dict):
        return None
    return raw


def outfits_for(
    char_id: str, entities: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return outfits available to a character: their linked ones first
    (from properties.outfits[]), then every generic outfit.

    A "generic" outfit is one whose properties.owner is missing, empty,
    or "generic" — the convention used by data/outfits/*.json. The
    Scene staging panel uses this list to populate the per-character
    outfit dropdown, so a user picking Nadia sees Nadia's wardrobe
    first and can fall back to the cross-character generics.

    Each entry carries an ``is_accessory`` flag derived from the
    outfit's ``properties.is_accessory``. The Scene staging panel
    filters on this — primary-outfit picker shows only non-accessory
    entries, accessory multi-checkbox row shows only accessory entries.
    """
    def _slots_of(ent: dict[str, Any]) -> dict[str, int]:
        raw = (ent.get("properties") or {}).get("clothing_slots") or {}
        if not isinstance(raw, dict):
            return {}
        out_slots: dict[str, int] = {}
        for k, v in raw.items():
            if not isinstance(k, str):
                continue
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n in (1, 2, 3):
                out_slots[k.lower()] = n
        return out_slots

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    char = entities.get(char_id) or {}
    # Owners matching THIS character (either by id, or by template
    # id when the entity is an instance of some other template) also
    # surface. Lets male outfits with `owner: "generic_blonde_guy"`
    # appear when the focal is the user instance entity templated
    # from generic_blonde_guy, without making them generic-and-
    # visible-to-everyone.
    template_id = char.get("_template_id") if isinstance(char.get("_template_id"), str) else None
    owner_match: set[str] = {char_id.lower()}
    if template_id:
        owner_match.add(template_id.lower())
    # B-sides surface their A-side's outfits too (A-side-owned outfits via
    # owner_match below; A-side's explicitly-linked outfits via the merged
    # list further down).
    from . import bside
    owner_match |= bside.owner_aliases(char, entities)
    def _partial_label(ent: dict[str, Any]) -> str | None:
        raw = (ent.get("properties") or {}).get("partial_label")
        return raw.strip() if isinstance(raw, str) and raw.strip() else None

    def _is_accessory(ent: dict[str, Any]) -> bool:
        return bool((ent.get("properties") or {}).get("is_accessory"))

    def _is_under(ent: dict[str, Any]) -> bool:
        return bool((ent.get("properties") or {}).get("under"))

    def _color(ent: dict[str, Any]) -> str:
        c = (ent.get("properties") or {}).get("color") or ""
        return c.strip() if isinstance(c, str) else ""

    def _is_templated(ent: dict[str, Any]) -> bool:
        """True iff the outfit's text uses any {color}/{material}/{fit}/
        {style} placeholder. Lets the staging UI hide the Color input
        for outfits where overriding wouldn't change anything."""
        props = ent.get("properties") or {}
        bodies: list[str] = []
        for key in ("intact_description", "concise_description", "description"):
            v = props.get(key) or ent.get(key)
            if isinstance(v, str):
                bodies.append(v)
        cov = props.get("coverage") or {}
        if isinstance(cov, dict):
            for part in cov.values():
                if isinstance(part, dict):
                    d = part.get("description")
                    if isinstance(d, str):
                        bodies.append(d)
        joined = " ".join(bodies)
        return any(tag in joined for tag in ("{color}", "{material}", "{fit}", "{style}"))

    def _entry(oid: str, ent: dict[str, Any], generic: bool) -> dict[str, Any]:
        return {
            "id": oid,
            "name": ent.get("name") or oid,
            "generic": generic,
            "is_accessory": _is_accessory(ent),
            "under": _is_under(ent),
            "templated": _is_templated(ent),
            "default_color": _color(ent),
            "clothing_slots": _slots_of(ent),
            "partial_label": _partial_label(ent),
        }

    # Linked outfits: the A-side's (for a B-side) merged ahead of the
    # character's own, so a B-side lists its A-side's real outfits plus any
    # outfits unique to itself.
    for oid in bside.merged_outfit_ids(char, entities):
        if not isinstance(oid, str) or oid in seen:
            continue
        ent = entities.get(oid)
        if not ent or ent.get("type") != "outfit":
            continue
        seen.add(oid)
        out.append(_entry(oid, ent, generic=False))
    for ent in entities.values():
        if not isinstance(ent, dict) or ent.get("type") != "outfit":
            continue
        oid = ent.get("id")
        if not oid or oid in seen:
            continue
        owner = (ent.get("properties") or {}).get("owner") or ""
        if isinstance(owner, str):
            owner_norm = owner.strip().lower()
        else:
            owner_norm = ""
        # Surface outfits that are: generic / unowned, OR owned by
        # this entity (or by the template it was instanced from).
        if owner_norm and owner_norm != "generic" and owner_norm not in owner_match:
            continue
        seen.add(oid)
        out.append(_entry(oid, ent, generic=owner_norm in ("", "generic")))
    return out


def resolve_setup(
    scenario: dict[str, Any], setup_id: str | None = None
) -> dict[str, Any]:
    """Return a fully-resolved setup. Falls back to the first setup."""
    setups = setup_list(scenario)
    if setup_id:
        for s in setups:
            if s["id"] == setup_id:
                return s
    return setups[0]


def _legacy_default_setup(scenario: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": DEFAULT_SETUP_ID,
        "name": "Default",
        "description": "",
    }


def _resolve(scenario: dict[str, Any], setup: dict[str, Any]) -> dict[str, Any]:
    """Apply inheritance from scenario → setup."""
    base_instructions = (scenario.get("scenario_instructions") or "").strip()
    append = (setup.get("scenario_instructions_append") or "").strip()
    if base_instructions and append:
        instructions = base_instructions + "\n\n" + append
    else:
        instructions = append or base_instructions

    fm: dict[str, str] = {}
    sc_first = scenario.get("first_messages") or {}
    if isinstance(sc_first, dict):
        fm.update({k: v for k, v in sc_first.items() if isinstance(v, str)})
    su_first = setup.get("first_messages") or {}
    if isinstance(su_first, dict):
        fm.update({k: v for k, v in su_first.items() if isinstance(v, str)})

    # Prompt fields the setup may override on top of the scenario.
    sys_char = _pick_str(setup.get("system_prompt_character"), scenario.get("system_prompt_character"))
    sys_narr = _pick_str(setup.get("system_prompt_narrator"), scenario.get("system_prompt_narrator"))
    author_note = _pick_str(setup.get("author_note"), scenario.get("author_note"))
    post_history = _pick_str(
        setup.get("post_history_instructions"), scenario.get("post_history_instructions")
    )

    depth_raw = setup.get("author_note_depth")
    if depth_raw is None:
        depth_raw = scenario.get("author_note_depth")
    try:
        author_note_depth = int(depth_raw) if depth_raw is not None else None
    except (TypeError, ValueError):
        author_note_depth = None

    return {
        "id": setup.get("id") or DEFAULT_SETUP_ID,
        "name": setup.get("name") or setup.get("id") or "Default",
        "description": setup.get("description") or "",
        "opening_prompt": (
            setup.get("opening_prompt")
            or scenario.get("opening_prompt")
            or scenario.get("description")
            or ""
        ),
        "scenario_instructions": instructions,
        "scenario_instructions_base": base_instructions,
        "scenario_instructions_append": append,
        "first_messages": fm,
        "user_persona": _resolve_user_persona(scenario, setup),
        "state": (setup.get("state") or "").strip(),
        "starting_state": copy.deepcopy(scenario.get("starting_state") or {}),
        "system_prompt_character": sys_char,
        "system_prompt_narrator": sys_narr,
        "author_note": author_note,
        "author_note_depth": author_note_depth,
        "post_history_instructions": post_history,
        "scene_staging_fields": (
            copy.deepcopy(setup["scene_staging_fields"])
            if isinstance(setup.get("scene_staging_fields"), dict)
            else None
        ),
        # Modules: every registered module is now available on every
        # scenario (no whitelist). `default_modules` is the per-setup
        # pre-selection — the staging UI pre-checks those ids in the
        # modules picker. The legacy `available_modules` field on the
        # scenario is preserved for ordering hints but no longer
        # restricts which modules surface in the picker.
        "available_modules": _str_list(scenario.get("available_modules")),
        "default_modules": _str_list(setup.get("default_modules")),
        # Quests: ids active on branches born from this setup, plus who judges
        # an accusation (falls back to the scenario-level giver). Read back by
        # the pf1e quest engine from the active setup root's metadata.
        "default_quests": _str_list(setup.get("default_quests")),
        "quest_giver": _pick_str(setup.get("quest_giver"), scenario.get("quest_giver")),
        # Optional tactical-grid seed (pf1e Grid mode): {width,height,units:[...]}.
        "grid": copy.deepcopy(setup.get("grid")) if isinstance(setup.get("grid"), dict) else None,
        # Optional Grid Message (pf1e): the same grid seed, but delivered as a
        # message node after the opening so the encounter is a rewindable block
        # (entered here, closed by a Grid Response). {intro?, ...grid config}.
        "grid_message": copy.deepcopy(setup.get("grid_message")) if isinstance(setup.get("grid_message"), dict) else None,
        # Optional cast-sourced grid (pf1e): a text-first scene with no static
        # units that CAN escalate — the Grid toggle seeds the encounter from the
        # pf1e actors present in the room. {hidden?:[ids], intro?}.
        "grid_from_cast": copy.deepcopy(setup.get("grid_from_cast")) if isinstance(setup.get("grid_from_cast"), dict) else None,
        # Optional shop config (pf1e builder): {price_mult,allow,deny,overrides,gold}.
        "shop": copy.deepcopy(setup.get("shop")) if isinstance(setup.get("shop"), dict) else None,
        # Optional NPC world-sim seed (pf1e): {npcs:{id:{role,knows,...}}}.
        "npc_sim": copy.deepcopy(setup.get("npc_sim")) if isinstance(setup.get("npc_sim"), dict) else None,
    }


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, str) and x]




def _pick_str(*candidates: Any) -> str:
    """Return the first non-empty string in candidates; "" otherwise."""
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c
    return ""


def _resolve_user_persona(
    scenario: dict[str, Any], setup: dict[str, Any]
) -> dict[str, Any]:
    """Resolve the user persona for an active setup.

    Three sources, applied in order (later wins):
      1. ``scenario.user_persona`` — legacy scenario-wide default.
      2. ``setup.user_persona_id`` — reference into
         ``scenario.user_personas[]`` (the canonical preset list).
         This is the preferred path for scenarios that maintain
         personas as a single shared list.
      3. ``setup.user_persona`` — legacy inline override on the setup.

    Returns a flat {name, description, ...} dict suitable for stamping
    onto ``settings.user_persona`` (and via path-replay, onto the
    ``user`` instance entity).

    Scenarios that set ``user_personas_are_roles: true`` at the top
    level get a different treatment for the user_personas[] preset:
    the preset's ``name`` / ``description`` are stamped as
    ``role`` / ``role_description`` instead of overwriting the user's
    own name / description. Lets a scenario declare "Alex playing a
    Federation liaison" rather than "the user IS Federation liaison."
    Other paths (scenario.user_persona, setup.user_persona) keep
    legacy semantics.
    """
    out: dict[str, Any] = {"name": "User", "description": ""}
    sc_up = scenario.get("user_persona")
    if isinstance(sc_up, dict):
        out.update({k: v for k, v in sc_up.items() if isinstance(v, str)})

    personas_are_roles = bool(scenario.get("user_personas_are_roles"))

    pid = setup.get("user_persona_id")
    if isinstance(pid, str) and pid:
        personas = scenario.get("user_personas") or []
        for p in personas:
            if isinstance(p, dict) and p.get("id") == pid:
                if personas_are_roles:
                    # Role overlay: the preset's name/description land
                    # on user.properties.role / role_description.
                    # User's own name + description stay as whatever
                    # the user card / earlier sources set them to.
                    role_label = p.get("name")
                    role_desc = p.get("description")
                    if isinstance(role_label, str) and role_label.strip():
                        out["role"] = role_label.strip()
                    if isinstance(role_desc, str):
                        out["role_description"] = role_desc.strip()
                    # Other string fields on the preset pass through
                    # under their own keys (lets authors stash
                    # arbitrary role-attached state, e.g., "clearance").
                    for k, v in p.items():
                        if (
                            isinstance(v, str)
                            and k not in ("id", "label", "name", "description")
                        ):
                            out[k] = v
                else:
                    out.update({
                        k: v for k, v in p.items()
                        if isinstance(v, str) and k not in ("id", "label")
                    })
                break

    su_up = setup.get("user_persona")
    if isinstance(su_up, dict):
        out.update({k: v for k, v in su_up.items() if isinstance(v, str)})
    return out


# ---------------------------------------------------------------------------
# Edit interpretation
#
# Setup `state` is a free-text block in the same grammar narrators emit for
# message-edit directives. We parse it through the existing extract_edits()
# so authors only have to learn one syntax. The edits split into two
# buckets:
#
#   user_edits   — target the user persona (id == "user"); applied to
#                  settings.user_persona, NOT to instance entities.
#   entity_edits — everything else; for the active setup these get fed
#                  through narrator_apply.apply_edits() so the instance
#                  reflects the [set] / [outfit] / [move] state from
#                  turn 0.
# ---------------------------------------------------------------------------


def parse_setup_state(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (user_edits, entity_edits) parsed from a setup's `state` block.

    A `user`-targeted edit is SPLIT by :func:`_split_user_edit`: its
    ``properties`` subtree (a module sheet like ``properties.pf1e``, or any
    other module's instance data) is routed to ``entity_edits`` — applied to
    the ``user`` instance entity through the normal entity-edit pipeline —
    while its flat persona fields (``name`` / ``description`` / ``role`` /
    ``role_description`` and arbitrary role flags) stay in ``user_edits``.
    The split is module-generic: the WHOLE ``properties`` subtree is routed,
    with no per-module special-casing. A plain persona edit (no
    ``properties``) is unchanged, so existing persona staging is preserved.
    """
    if not text:
        return [], []
    _cleaned, edits = extract_edits(text)
    user_edits: list[dict[str, Any]] = []
    entity_edits: list[dict[str, Any]] = []
    for e in edits:
        target = _edit_target(e)
        if target == "user":
            persona_part, entity_part = _split_user_edit(e)
            if entity_part is not None:
                entity_edits.append(entity_part)
            if persona_part is not None:
                user_edits.append(persona_part)
        else:
            entity_edits.append(e)
    return user_edits, entity_edits


def _split_user_edit(
    edit: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Split a ``user``-targeted setup edit into (persona_edit, entity_edit).

    The ``properties`` subtree of a ``user`` edit describes the player's real
    INSTANCE ENTITY (e.g. ``properties.pf1e`` — a module character sheet), so
    it is routed to the ENTITY bucket and applied to the ``user`` instance via
    the normal entity-edit pipeline. Everything else on the edit is persona
    identity (``name`` / ``description`` / ``role`` / ``role_description`` and
    any arbitrary role flags) and stays in the PERSONA bucket verbatim.

    Module-generic: the entire ``properties`` subtree is routed for ANY
    module — there is no ``if "pf1e"`` special-case. The extracted subtree is
    always emitted as a ``patch`` on ``user.properties`` (never a wholesale
    ``replace``), so seeding a sheet deep-merges into the instance entity's
    properties without clobbering sibling entity fields.

    Returns ``(persona_edit_or_None, entity_edit_or_None)``:
      * a plain persona edit (no ``properties``) → ``(edit, None)`` unchanged;
      * a pure ``properties`` edit → ``(None, <properties patch>)``;
      * a mixed edit → ``(<flat remainder>, <properties patch>)``.
    """
    kind = edit.get("kind")
    if kind in ("patch", "replace"):
        data = edit.get("data")
        if not isinstance(data, dict) or "properties" not in data:
            return edit, None
        props = data.get("properties")
        entity_edit = {
            "kind": "patch",
            "id": "user",
            "data": {"properties": props},
        }
        remainder = {k: v for k, v in data.items() if k != "properties"}
        persona_edit: dict[str, Any] | None = None
        if remainder:
            persona_edit = {"kind": kind, "id": "user", "data": remainder}
        return persona_edit, entity_edit
    if kind == "unset":
        path = edit.get("path") or []
        if path and path[0] == "properties":
            return None, edit
        return edit, None
    # move / outfit (character-id targeted) or any other user edit: no
    # `properties` subtree, so it stays in the persona bucket unchanged.
    return edit, None


def _edit_target(edit: dict[str, Any]) -> str | None:
    kind = edit.get("kind")
    if kind in ("patch", "replace", "set", "unset"):
        return edit.get("id")
    if kind in ("move", "outfit"):
        return edit.get("character_id")
    return None


def apply_user_persona_edits(
    user_persona: dict[str, Any], edits: list[dict[str, Any]]
) -> dict[str, Any]:
    """Mutate `user_persona` in place from `[set user.X = ...]` directives.

    `extract_edits` shapes a `[set user.foo = bar]` line as a patch edit
    with id="user" and data={"foo": "bar"} (nested dotted paths produce
    nested dicts). We deep-merge the patch into user_persona so authors
    can stash arbitrary flags ("user.role" = "producer", "user.notes" =
    "...") alongside the standard name/description fields.
    """
    from .merge import deep_merge as _deep_merge

    for e in edits:
        kind = e.get("kind")
        if kind in ("patch", "replace"):
            data = e.get("data") or {}
            if isinstance(data, dict):
                if kind == "replace":
                    user_persona.clear()
                _deep_merge(user_persona, data)
        elif kind == "unset":
            path = e.get("path") or []
            cur: Any = user_persona
            for key in path[:-1]:
                if not isinstance(cur, dict) or key not in cur:
                    cur = None
                    break
                cur = cur[key]
            if isinstance(cur, dict) and path:
                cur.pop(path[-1], None)
    return user_persona


# ---------------------------------------------------------------------------
# Presence-only application for non-active setups.
#
# Sibling root setups can't all write to the shared instance — only the
# active setup's [set] / [unset] / [patch] edits get applied (to keep
# entity state coherent). But every setup root still needs an accurate
# presence_snapshot reflecting its move/outfit edits, so the UI shows the
# right rooms / outfits when the user navigates between root branches.
# ---------------------------------------------------------------------------


def compute_presence(
    baseline: dict[str, Any], edits: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return a copy of `baseline` (a presence_snapshot dict) with move /
    outfit edits applied. Does not touch any instance entities."""
    out = copy.deepcopy(baseline) if isinstance(baseline, dict) else {}
    presence = out.setdefault("presence", {})
    for e in edits:
        kind = e.get("kind")
        if kind == "move":
            cid = e.get("character_id")
            if not cid:
                continue
            row = presence.setdefault(cid, {})
            row["room"] = e.get("room")
            if e.get("location"):
                row["location"] = e.get("location")
        elif kind == "outfit":
            cid = e.get("character_id")
            if not cid:
                continue
            presence.setdefault(cid, {})["outfit"] = e.get("outfit_id")
    return out
