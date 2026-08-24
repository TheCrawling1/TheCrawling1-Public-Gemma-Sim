"""Core blocks shipped by the engine.

Imported for its side effect: each block decorator registers an entry
in `app.prompt.registry._REGISTRY`. The package's `__init__.py` imports
this module so registrations happen at first `from app import prompt`.

Block functions delegate to existing helpers in `personas.py` for the
actual rendering. The point of this file isn't new rendering logic —
it's the wiring that says WHICH blocks render and in what order.

Order slots (see registry.py for ranges):

  Character path: 10 system, 20 focal, 30 lore_before, 40 user, 50
  active_voice, 60 surroundings, 70 others_present, 130 items_in_scene,
  140 lore_after, 150 scenario, 160 scene_effects, 170 dev, 180
  wardrobe_overrides, 190 style_discipline.

  Narrator path: 10 system, 25 cast, 30 lore_before, 35 world, 140
  lore_after, 150 scenario, 160 scene_effects, 170 dev, 180
  wardrobe_overrides, 190 style_discipline.

  100-199 reserved for module blocks (life_sim's 120 + 121, etc.).

INTENTIONAL DIFFERENCES from the live `_assemble_*` paths:

  - wardrobe_overrides now applies to BOTH personas. Was narrator-only
    in personas.py — drift bug; the registry fixes it for free since
    there's a single declaration.
  - scene_effects now sits BEFORE dev for narrator too (it was after
    dev). Brings narrator into line with character ordering. Affects
    only conversations that use the scene_effects prefab.

Everything else is byte-identical (verified via
tools/verify_prompt_registry.py against conv_e3dd2d0487bd).
"""
from __future__ import annotations

from typing import Any

from .registry import Block, register


# ---------------------------------------------------------------------------
# 10 — system prompt (both personas)
# ---------------------------------------------------------------------------


@register(id="system_prompt", order=10)
def _block_system_prompt(ctx):
    from ..personas import (
        DEFAULT_SYSTEM_CHARACTER, DEFAULT_SYSTEM_NARRATOR, apply_macros,
    )
    if ctx.persona == "narrator":
        template = (
            (ctx.settings.get("system_prompt_narrator") or "").strip()
            or DEFAULT_SYSTEM_NARRATOR
        )
    else:
        template = (
            (ctx.settings.get("system_prompt_character") or "").strip()
            or DEFAULT_SYSTEM_CHARACTER
        )
    return Block(
        label="System prompt",
        content=apply_macros(template, ctx.macros),
        section=None,
    )


# ---------------------------------------------------------------------------
# 20 — focal character "You — name" (character only)
# ---------------------------------------------------------------------------


@register(id="focal_character", order=20, applies_to=("character",))
def _block_focal_character(ctx):
    if not ctx.focal:
        return None
    from ..personas import _character_card
    presence_outfit = (ctx.presence or {}).get("outfit")
    text = _character_card(
        ctx.focal, ctx.entities, ctx.macros,
        current_outfit_override=presence_outfit,
    )
    name = ctx.focal.get("name") or ctx.focal_id
    return Block(
        label=f"You — {name}",
        content=text,
        section=f"You — {name}",
    )


# ---------------------------------------------------------------------------
# 25 — cast list (narrator only)
# ---------------------------------------------------------------------------


@register(id="cast", order=25, applies_to=("narrator",))
def _block_cast(ctx):
    from ..personas import _cast_summary
    text = _cast_summary(ctx.entities, ctx.macros)
    if not text:
        return None
    return Block(label="Cast", content=text, section="Cast")


@register(id="absent_cast", order=75)
def _block_absent_cast(ctx):
    """Name scenario-pool characters staged out of this branch so the
    model doesn't pull them in off another character's bio prose.
    Renders for both narrator and character prompts; empty (no block) on
    non-staged branches where the whole pool is present."""
    from ..personas import _absent_cast_note
    text = _absent_cast_note(ctx.conversation, ctx.entities, ctx.macros)
    if not text:
        return None
    return Block(label="Not present", content=text, section="Not present")


# ---------------------------------------------------------------------------
# 30 — lore (before char defs) (both)
# ---------------------------------------------------------------------------


@register(id="lore_before", order=30)
def _block_lore_before(ctx):
    from ..personas import _activated_lore, _format_lore
    lore = _activated_lore(ctx.entities, ctx.history)
    text = _format_lore(lore.get("before_char") or [], ctx.macros)
    if not text:
        return None
    return Block(
        label="Lore (before char defs)",
        content=text,
        section="Lore — before",
    )


# ---------------------------------------------------------------------------
# 35 — world summary (narrator only)
# ---------------------------------------------------------------------------


@register(id="world", order=35, applies_to=("narrator",))
def _block_world(ctx):
    from ..personas import _world_summary
    text = _world_summary(ctx.entities, ctx.macros)
    if not text:
        return None
    return Block(label="World", content=text, section="World")


# ---------------------------------------------------------------------------
# 40 — user persona / user card (character only)
# ---------------------------------------------------------------------------


@register(id="user_persona", order=40, applies_to=("character",))
def _block_user_persona(ctx):
    """Render the user the same way NPCs render — structured card from
    the user instance entity when available; legacy free-text fallback
    for conversations without a structured user entity. Locational-
    memory gating drops the block when the user isn't co-located with
    the focal.

    Mirrors personas.py:594-639 and the locational-memory drop at
    personas.py:664-667.
    """
    from ..personas import (
        _character_card, _format_state_value, _latest_presence_for,
        _scene_gated_out, apply_macros, perceives_user_identity,
    )
    user_persona = ctx.settings.get("user_persona") or {}
    user_entity = ctx.entities.get("user") or {}
    user_name = ctx.user_name  # already the descriptor if this focal doesn't know them

    # Locational-memory drop FIRST: if the user isn't co-located, the focal can't
    # perceive them at all — no block, whether or not they know who they are.
    if ctx.settings.get("locational_memory", True):
        user_presence_for_scene = _latest_presence_for(ctx.history, "user")
        if ctx.presence and _scene_gated_out(ctx.presence, user_presence_for_scene):
            return None

    # Perceive-only: a focal who doesn't KNOW the user (a stranger) is shown only
    # what they can see — visible body/clothing — never the name, the authored
    # identity/backstory, or narrator-tracked state.
    known = perceives_user_identity(
        ctx.focal_id, ctx.conversation,
        (user_persona.get("role") or "").strip().lower())
    if not known:
        visible = ""
        try:
            from ..clothing_v2 import compose_body_description_v2
            visible = "; ".join(
                t for _, t in compose_body_description_v2(user_entity, ctx.entities) if t
            ).strip()
        except Exception:
            visible = ""
        if not visible and isinstance(user_persona.get("appearance"), str):
            visible = user_persona["appearance"].strip()
        lines = []
        if visible:
            lines.append("What you can see: " + apply_macros(visible, ctx.macros))
        lines.append(
            "You do not know this person, and you do not know their name. Go on "
            "what you can see — do not use a name or personal details you were "
            "never told.")
        return Block(
            label=f"Unknown person ({user_name})",
            content="\n\n".join(lines),
            section="Someone you don't know",
        )

    has_structured_user = bool(
        isinstance(user_entity, dict)
        and isinstance((user_entity.get("properties") or {}), dict)
        and (user_entity.get("properties") or {}).get("body_parts")
    )

    if has_structured_user:
        user_presence = _latest_presence_for(ctx.history, "user")
        user_presence_outfit = (user_presence or {}).get("outfit")
        user_card = _character_card(
            user_entity, ctx.entities, ctx.macros,
            current_outfit_override=user_presence_outfit,
        )
        appearance_override = user_persona.get("appearance")
        if isinstance(appearance_override, str) and appearance_override.strip():
            user_card += f"\n\nCurrently visible: {appearance_override.strip()}"
        user_desc = user_card
    else:
        user_desc_parts: list[str] = []
        base_desc = (user_persona.get("description") or "").strip()
        if base_desc:
            user_desc_parts.append(base_desc)
        appearance = (
            (user_persona.get("appearance") or "").strip()
            if isinstance(user_persona.get("appearance"), str) else ""
        )
        if appearance:
            user_desc_parts.append(f"Currently visible: {appearance}")
        # Surface narrator-tracked user state (mood, role, etc.).
        _USER_RENDERED = {
            "name", "description", "appearance", "card_id", "id", "type",
            "tags", "children", "properties", "_template_id", "example_text",
        }
        extras = [
            (k, v) for k, v in user_persona.items()
            if k not in _USER_RENDERED and v not in (None, "", [], {})
        ]
        if extras:
            user_desc_parts.append(
                "Current state (narrator-tracked):\n"
                + "\n".join(f"- {k}: {_format_state_value(v)}" for k, v in extras)
            )
        user_desc = "\n\n".join(user_desc_parts)

    if not user_desc:
        return None

    user_desc = apply_macros(user_desc, ctx.macros)
    return Block(
        label=f"User persona ({user_name})",
        content=user_desc,
        section=f"The user — {user_name}",
    )


# ---------------------------------------------------------------------------
# 50 — active voice (character only)
# ---------------------------------------------------------------------------


@register(id="active_voice", order=50, applies_to=("character",))
def _block_active_voice(ctx):
    """When the user is currently speaking AS another character, tell
    the responder so they don't treat that line as the user's own
    voice. Mirrors personas.py:676-688."""
    if not ctx.focal_id:
        return None
    from ..personas import _user_speaking_as
    speaking_as = _user_speaking_as(
        ctx.history, ctx.entities, exclude_id=ctx.focal_id,
    )
    if not speaking_as:
        return None
    speaker_name = (ctx.entities.get(speaking_as) or {}).get("name") or speaking_as
    # Dev-panel-friendly long form (mirrors pieces.append at line 678).
    dev_content = (
        f"The user ({ctx.user_name}) is currently giving voice to "
        f"{speaker_name}. Treat the most recent {speaker_name} "
        f"turn(s) as words and actions of {speaker_name} herself "
        f"in this scene, not as the user speaking. Reply to "
        f"{speaker_name} as you would to any other present character."
    )
    # The system-text shape is the shorter system_parts variant (line 798-803).
    system_content = (
        f"The user is currently giving voice to {speaker_name}. "
        f"Treat the most recent {speaker_name} turn(s) as that character speaking, "
        f"not as the user."
    )
    return Block(
        label="Active voice",
        content=system_content,
        section="Active voice",
        # `pieces` gets the longer form via _block_active_voice_dev_only
        # below; the system_text gets the short version.
    )


# ---------------------------------------------------------------------------
# 45 — relationship standing (character only)
# ---------------------------------------------------------------------------

# Generic, module-free standings keyed by the core relationship-tag vocabulary.
# One sentence per word, addressed to the focal. This is what steers the LIVE
# model (the conditional pairs are only primer examples); without it a card
# written as "already familiar" would fight a stranger scene. Priority orders
# the tags so the most defining one speaks when several are present.
_REL_STANDING = {
    "hostile": "You regard {u} as an enemy or a threat — cold and sharp with them, unwilling to give them anything. If they ask your name or anything personal, you refuse outright or turn it back on them; you give them NO name at all.",
    "wary": "You know of {u} but do not trust them — guarded, watchful, keeping your distance. If they ask your name, where you're from, or anything personal, you deflect or decline; you do NOT give your name (not even a first name) or any private thing to someone you distrust.",
    "stranger": "You have only just met {u} and do not know or trust them. Be reserved and cautious. If they ask your name, where you're from, or anything personal, you do NOT answer it straight — you decline, deflect, or turn the question back on them, kindly but firmly. In particular you do NOT tell them your name — not your full name, and not even your first name — to someone you met moments ago; refusing a name is normal and expected, not rude. Any warmth or openness is earned over time, not offered up front — do not act as though you already know them.",
    "lover": "{u} is your lover — intimate and familiar with them, physically and emotionally open.",
    "crush": "You have feelings for {u} you haven't fully admitted — a little flustered, a little forward, more aware of them than you let on.",
    "close": "You are close to {u} — relaxed, affectionate, and unguarded with them.",
    "friend": "{u} is a friend — warm, open, and at ease with them.",
    "acquaintance": "You've met {u} a handful of times — polite and friendly enough, but still finding your footing; not close yet.",
}
_REL_PRIORITY = ("hostile", "wary", "stranger", "lover", "crush", "close", "friend", "acquaintance")


@register(id="relationship_standing", order=45, applies_to=("character",))
def _block_relationship_standing(ctx):
    """State the focal's current standing toward the user in one line, read
    from the same generic relationship channel the dialogue-pair selector uses
    (`resolve_relationship_tags`). Silent when there's no relationship tag."""
    if not ctx.focal_id:
        return None
    from ..personas import resolve_relationship_tags, apply_macros
    user_role = ((ctx.settings.get("user_persona") or {}).get("role") or "").strip().lower()
    tags = resolve_relationship_tags(ctx.focal_id, ctx.conversation, user_role)
    if not tags:
        return None
    chosen = next((t for t in _REL_PRIORITY if t in tags), None)
    if not chosen:
        return None
    line = _REL_STANDING[chosen].format(u=ctx.user_name or "them")
    return Block(
        label="Relationship",
        content=apply_macros(line, ctx.macros),
        section="Relationship",
    )


# ---------------------------------------------------------------------------
# 60 — surroundings (character only)
# ---------------------------------------------------------------------------


@register(id="surroundings", order=60, applies_to=("character",), tags=("environmental",))
def _block_surroundings(ctx):
    from ..personas import _surroundings_text
    text = _surroundings_text(ctx.entities, ctx.presence or {}, ctx.macros)
    if not text:
        return None
    return Block(label="Surroundings", content=text, section="Surroundings")


# ---------------------------------------------------------------------------
# 70 — others present (character only)
# ---------------------------------------------------------------------------


@register(id="others_present", order=70, applies_to=("character",), tags=("environmental",))
def _block_others_present(ctx):
    if not ctx.focal_id:
        return None
    from ..personas import _others_present_text
    text = _others_present_text(
        ctx.focal_id, ctx.entities, ctx.history, ctx.macros,
    )
    if not text:
        return None
    return Block(label="Others present", content=text, section="Others present")


# ---------------------------------------------------------------------------
# 130 — items in scene (character only)
# ---------------------------------------------------------------------------


@register(id="items_in_scene", order=130, applies_to=("character",), tags=("environmental",))
def _block_items_in_scene(ctx):
    """Object entities in the cast that aren't equipped to a character.
    Equipped objects render inside the owner's character card via
    `_equipped_text` instead. Mirrors personas.py:839-847."""
    from ..personas import _object_block
    items = [
        e for e in ctx.entities.values()
        if e.get("type") == "object"
        and e.get("id") != "user"
        and not (e.get("properties") or {}).get("equipped_to")
    ]
    if not items:
        return None
    text = "\n".join(_object_block(o, ctx.macros, ctx.entities) for o in items)
    return Block(label="Items in scene", content=text, section="Items in scene")


# ---------------------------------------------------------------------------
# 140 — lore (after char defs) (both)
# ---------------------------------------------------------------------------


@register(id="lore_after", order=140)
def _block_lore_after(ctx):
    from ..personas import _activated_lore, _format_lore
    lore = _activated_lore(ctx.entities, ctx.history)
    text = _format_lore(lore.get("after_char") or [], ctx.macros)
    if not text:
        return None
    return Block(
        label="Lore (after char defs)",
        content=text,
        section="Lore — after",
    )


# ---------------------------------------------------------------------------
# 150 — scenario instructions (both)
# ---------------------------------------------------------------------------


@register(id="scenario_instructions", order=150)
def _block_scenario(ctx):
    from ..personas import apply_macros
    text = apply_macros(
        (ctx.settings.get("scenario_instructions") or "").strip(),
        ctx.macros,
    )
    if not text:
        return None
    return Block(label="Scenario instructions", content=text, section="Scenario")


# ---------------------------------------------------------------------------
# 155 — scenario mode (both) — a scenario-wide framing/instructions
# variant chosen from the scenario_modes prefab. Sits just before
# scene_effects (160) so a picked "mode" frames the scene ahead of any
# per-cast scene effects. Distinct field from scene_effects so the two
# prefabs don't clobber each other.
# ---------------------------------------------------------------------------


@register(id="scenario_mode", order=155, tags=("environmental",))
def _block_scenario_mode(ctx):
    """Scenario-wide `{label, text}` written by the scenario_modes prefab
    (kind `scenario_variant_select`) into `properties.scenario_mode` on
    every picked character. Same render shape as scene_effects: narrator
    pulls any character's non-empty copy (they all carry the same mode
    payload); the focal reads its own."""
    def _payload(ent):
        sm = (ent.get("properties") or {}).get("scenario_mode")
        if isinstance(sm, dict) and isinstance(sm.get("text"), str) and sm["text"].strip():
            return sm
        return None

    if ctx.persona == "narrator":
        for ent in ctx.entities.values():
            if ent.get("type") != "character":
                continue
            sm = _payload(ent)
            if sm:
                label = sm.get("label") or "Scenario mode"
                return Block(label=label, content=sm["text"].strip(), section=label)
        return None

    if not ctx.focal:
        return None
    sm = _payload(ctx.focal)
    if not sm:
        return None
    label = sm.get("label") or "Scenario mode"
    return Block(label=label, content=sm["text"].strip(), section=label)


# ---------------------------------------------------------------------------
# 160 — scene effects (both — unified order)
# ---------------------------------------------------------------------------


@register(id="scene_effects", order=160, tags=("environmental",))
def _block_scene_effects(ctx):
    """The staging-panel scene_effects prefab patches a `{label, text}`
    block onto every picked character. For the narrator we pull any
    char's copy (they all carry the same payload); for the focal we
    read its own copy. Mirrors personas.py:521-533 (narrator) and
    personas.py:858-865 (character).

    NOTE — intentional behaviour change: narrator's scene_effects now
    sits BEFORE dev (160 < 170) rather than after. Brings narrator in
    line with the character path order.
    """
    if ctx.persona == "narrator":
        target_dict: dict[str, Any] | None = None
        for ent in ctx.entities.values():
            if ent.get("type") != "character":
                continue
            se = (ent.get("properties") or {}).get("scene_effects")
            if (
                isinstance(se, dict)
                and isinstance(se.get("text"), str)
                and se["text"].strip()
            ):
                target_dict = se
                break
        if not target_dict:
            return None
        label = target_dict.get("label") or "Scene effects"
        return Block(
            label=label,
            content=target_dict["text"].strip(),
            section=label,
        )

    # Character path: focal's own copy.
    if not ctx.focal:
        return None
    se = (ctx.focal.get("properties") or {}).get("scene_effects")
    if not isinstance(se, dict):
        return None
    text = se.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    label = se.get("label") or "Scene effects"
    return Block(label=label, content=text.strip(), section=label)


# ---------------------------------------------------------------------------
# 161 — character effect (both) — a per-character transform/condition note,
# a sibling channel to scene_effects so an effect and an author's note can
# coexist on the same character without clobbering each other.
# ---------------------------------------------------------------------------


@register(id="character_effect", order=161, tags=("environmental",))
def _block_character_effect(ctx):
    """Per-character `{label, text}` written by the per_character_select
    prefab into `properties.character_effect`. Focal reads its own copy;
    narrator pulls any character's non-empty copy. Mirrors scene_effects
    but on a distinct field so the two don't overwrite one another."""
    def _payload(ent):
        ce = (ent.get("properties") or {}).get("character_effect")
        if isinstance(ce, dict) and isinstance(ce.get("text"), str) and ce["text"].strip():
            return ce
        return None

    if ctx.persona == "narrator":
        for ent in ctx.entities.values():
            if ent.get("type") != "character":
                continue
            ce = _payload(ent)
            if ce:
                label = ce.get("label") or "Effect"
                return Block(label=label, content=ce["text"].strip(), section=label)
        return None

    if not ctx.focal:
        return None
    ce = _payload(ctx.focal)
    if not ce:
        return None
    label = ce.get("label") or "Effect"
    return Block(label=label, content=ce["text"].strip(), section=label)


# ---------------------------------------------------------------------------
# 170 — dev panel instructions (both)
# ---------------------------------------------------------------------------


@register(id="dev_panel", order=170)
def _block_dev_panel(ctx):
    from ..personas import apply_macros
    text = apply_macros(
        ctx.settings.get("dev_panel_instructions"),
        ctx.macros,
    )
    if not text:
        return None
    return Block(label="Dev panel", content=text, section="Dev Instructions")


# ---------------------------------------------------------------------------
# 180 — wardrobe overrides (both — drift fix)
# ---------------------------------------------------------------------------


@register(id="wardrobe_overrides", order=180, tags=("environmental",))
def _block_wardrobe_overrides(ctx):
    """Sprite-renderer slot override instructions. Previously emitted
    only in `_assemble_narrator` (line 534). Now applies to character
    too — drift fix: characters can emit `[set <char>.clothing_overrides.
    <slot> = 2]` overrides, but without this block in their prompt they
    had no documentation of the directive shape.
    """
    from ..personas import clothing_overrides_instruction
    # Characters voicing a beat only ever change how their OWN garment sits
    # (shirt pulled up, bra off) — the clothing_overrides directive alone.
    # They get the compact version; the narrator, which manages the whole
    # scene, keeps the full manual (transparency, recolor, accessories,
    # body marks). Saves ~950 tokens per character turn, stacking across
    # every partner in a multi-response chain, with no loss of the
    # different-clothing-states capability.
    text = clothing_overrides_instruction(
        ctx.entities, concise=(ctx.persona == "character"), self_id=ctx.focal_id,
    )
    if not text:
        return None
    return Block(
        label="Wardrobe overrides",
        content=text,
        section="Wardrobe overrides",
    )


# ---------------------------------------------------------------------------
# 190 — style discipline (both, last)
# ---------------------------------------------------------------------------


@register(id="known_only", order=185, applies_to=("character",))
def _block_known_only(ctx):
    """Anti-confabulation steer. Locational memory and the acquaintance layer
    keep information a character never perceived OUT of the prompt — but an
    absent fact doesn't stop the model inventing one when asked point-blank.
    This tells the character to admit ignorance instead of filling the gap, so
    "blocked information" reads as blocked in the reply, not just the context.
    Placed late (high recency) for adherence."""
    return Block(
        label="Only what you know",
        content=(
            "You know only what you have seen, heard, or been told in this scene. "
            "If you are asked about something you have no knowledge of — a place you "
            "didn't go, a note or sight you weren't there for, words spoken where you "
            "weren't present, a name you were never told — say you don't know, or that "
            "you weren't there. Do NOT invent specifics to fill the gap. A character "
            "admitting they don't know is correct; making something up to seem helpful "
            "is not."
        ),
        section="Only what you know",
    )


@register(id="character_memory", order=186, applies_to=("character",))
def _block_character_memory(ctx):
    """Durable facts this character has learned (see
    docs/character_memory_design.md). Injected late so a salient fact — a
    password, a name, a task — is present in the prompt even after the raw turn
    that taught it has been truncated out of the context window. Reads the
    accumulating branch-local memory, not the message window, so it survives
    truncation and a rewind drops branch-learned facts."""
    if not ctx.focal_id:
        return None
    from ..effective import memory_for_path
    from ..personas import apply_macros
    try:
        facts = memory_for_path(ctx.conversation, ctx.focal_id)
    except Exception:
        facts = []
    if not facts:
        return None
    lines = []
    for rec in facts:
        t = apply_macros((rec.get("text") or "").strip(), ctx.macros)
        if not t:
            continue
        room = (rec.get("where") or {}).get("room") if isinstance(rec.get("where"), dict) else None
        lines.append(f"- {t}" + (f" (you learned this in the {room.replace('_', ' ')})" if room else ""))
    if not lines:
        return None
    return Block(label="What you know", content="\n".join(lines), section="What you know")


@register(
    id="style_discipline",
    order=190,
    applies_to=("character", "narrator", "narrator_edit", "setup"),
)
def _block_style_discipline(ctx):
    """Banned-phrase block. Already carries its own bracketed header
    in the helper output, so emit with section=None to avoid wrapping
    twice.

    Applies to the standalone aux prompts that historically appended it
    (`narrator_edit`, `setup`) as well as the two live personas — single
    source of truth. (narrator_add carries no style-discipline block, by
    its own design, so it's omitted here.) Wrapped in try/except so a
    no-app-context test harness degrades to "no block" the same way the
    old `f"{system}\\n\\n{sd}"` append did inside its own try."""
    try:
        from ..personas import _style_discipline_block
        text = _style_discipline_block()
    except Exception:
        return None
    if not text:
        return None
    return Block(label="Style discipline", content=text, section=None)
