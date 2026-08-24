"""PromptContext: the bundle every block receives.

Replaces the loose `(conversation, entities, history, settings, ctx,
character_id)` argument soup that `personas._assemble_character` and
`_assemble_narrator` carried around. Built once at the top of
`assemble()` and passed unchanged to each block.

`build_context()` is the one-place setup that resolves path-effective
state (entities + persona + scenario instructions), runs branch
isolation, and computes the macro substitutions ({{user}}, {{char}}).
Each call is cheap — it pulls from the in-memory conversation dict
and re-walks the active path, no I/O beyond what the live engine
does today.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PromptContext:
    """Everything a block needs to render.

    Attributes carry the same semantics they have in `personas.py`
    today — only the packaging is different.

    `persona` is the prompt KIND ("character" or "narrator"), not a
    character id. `focal_id` / `focal` are populated only for the
    character kind; for narrator they're None.

    `entities` is the path-effective + branch-filtered map (the same
    one `_assemble_character` builds). `history` is the leaf→root
    message list (the same one `path_to_root` returns).

    `macros` carries `{user_name, char_name, user_persona}` for
    `apply_macros()` substitution. `presence` is the focal character's
    latest presence-snapshot row (room / location / outfit), or `{}`
    for narrator.
    """
    conversation: dict[str, Any]
    persona: str
    focal_id: str | None
    focal: dict[str, Any] | None
    entities: dict[str, dict[str, Any]]
    history: list[dict[str, Any]]
    settings: dict[str, Any]
    macros: dict[str, Any]
    presence: dict[str, Any]
    leaf_id: str

    @property
    def user_name(self) -> str:
        return (self.macros.get("user_name") or "User") if self.macros else "User"

    @property
    def char_name(self) -> str:
        if not self.macros:
            return "Narrator"
        return self.macros.get("char_name") or "Narrator"


def build_context(
    conversation: dict[str, Any],
    *,
    persona: str,
    focal_id: str | None = None,
    leaf_id: str | None = None,
) -> PromptContext:
    """Build a PromptContext for the assembler.

    `persona` is "character" or "narrator". `focal_id` is the
    character id when persona="character", ignored otherwise.

    Mirrors the setup work `personas.assemble_prompt` does at lines
    381-423: resolve the path-effective entities, branch-filter,
    compute user_persona macros, walk history. Anything beyond that
    (per-persona routing, system_parts building) is the registry's job.
    """
    from ..effective import (
        branch_filter,
        effective_entities_at,
        effective_scenario_instructions,
        effective_user_persona,
    )
    from ..personas import _latest_presence_for, path_to_root

    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    history = path_to_root(conversation, leaf_id) if leaf_id else []

    entities = effective_entities_at(conversation, leaf_id)
    entities = branch_filter(conversation, leaf_id, entities)

    # Keep the focal in the entity map even if branch_filter dropped
    # them — the caller picked them deliberately and the registry
    # needs to render their card. Mirrors the safety net at
    # personas.py:407.
    focal: dict[str, Any] | None = None
    if persona == "character" and focal_id:
        if focal_id not in entities:
            full = effective_entities_at(conversation, leaf_id)
            if focal_id in full:
                entities[focal_id] = full[focal_id]
        focal = entities.get(focal_id)
        if focal and focal.get("type") != "character":
            focal = None

    user_persona = effective_user_persona(conversation, leaf_id)
    user_entity = entities.get("user") or {}
    user_name = (
        (user_entity.get("name") or "").strip()
        or (user_persona.get("name") or "").strip()
        or "User"
    )
    char_name = (focal or {}).get("name") if focal else "Narrator"
    if not char_name:
        char_name = focal_id if focal_id else "Narrator"
    macros = {
        "user_name": user_name,
        "char_name": char_name,
        "user_persona": user_persona,
    }

    settings = dict(conversation.get("settings", {}) or {})
    settings["user_persona"] = user_persona
    settings["scenario_instructions"] = effective_scenario_instructions(
        conversation, leaf_id,
    )
    # Scene staging cast scoping is handled by branch_filter on the
    # entity map (effective cast already honors the staging picks +
    # replays cast_add/cast_remove), so the [Cast] block needs no
    # separate pick list here.

    presence = _latest_presence_for(history, focal_id) if focal_id else {}

    return PromptContext(
        conversation=conversation,
        persona=persona,
        focal_id=focal_id,
        focal=focal,
        entities=entities,
        history=history,
        settings=settings,
        macros=macros,
        presence=presence or {},
        leaf_id=leaf_id,
    )
