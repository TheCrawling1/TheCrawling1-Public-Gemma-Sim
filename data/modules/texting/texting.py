"""Texting module — backend.

Self-contained drop-in. Registers a single prompt-block filter that
drops engine blocks tagged ``environmental`` when the focal character
is responding to a user message marked as a text.

The module never writes new prompt blocks of its own — its whole
backend job is to GATE existing engine blocks. Each environmental
block has its own per-setting strip toggle so a user can keep some
context (e.g., keep wardrobe but drop surroundings) without forking
the prompt assembler.

The frontend half (`texting.js`) stamps
``metadata.modules.texting = {to: <char_id>}`` on the user message
before POST; this filter reads that metadata at prompt-assembly time
to decide whether to drop blocks.
"""
from __future__ import annotations

from typing import Any

from app.modules.api import is_active as _is_active, settings_for as _settings_for
from app.prompt import register_filter, register, Block, register_message_annotator


MODULE_ID = "texting"


def _settings(conversation: dict[str, Any], leaf_id: str | None = None) -> dict[str, Any]:
    """Effective settings for this branch — defaults filled in.

    Mirrors what other modules do: read module_settings off the
    active setup root and fall back to manifest defaults for any
    keys the user hasn't customized. Hardcoded fallback values
    match data/modules/texting/module.json — if you change the
    manifest defaults, change them here too.
    """
    raw = _settings_for(conversation, MODULE_ID, leaf_id) or {}
    return {
        "strip_surroundings":     raw.get("strip_surroundings", True),
        "strip_others_present":   raw.get("strip_others_present", True),
        "strip_items_in_scene":   raw.get("strip_items_in_scene", True),
        "strip_scene_effects":    raw.get("strip_scene_effects", True),
        "strip_wardrobe_overrides": raw.get("strip_wardrobe_overrides", True),
        "render_style":           raw.get("render_style", "sms_bubble"),
    }


# Map between engine block id and the corresponding "strip_X" setting.
# When the setting is True, the block is dropped for texting replies.
_BLOCK_TO_SETTING = {
    "surroundings":        "strip_surroundings",
    "others_present":      "strip_others_present",
    "items_in_scene":      "strip_items_in_scene",
    "scene_effects":       "strip_scene_effects",
    "wardrobe_overrides":  "strip_wardrobe_overrides",
}


def _focal_is_replying_to_text(ctx) -> bool:
    """Walk recent history leaf→root and return True iff the most
    recent USER message has ``metadata.modules.texting.to`` pointing
    at the focal character.

    The "most recent user message" is the last message authored by
    the user (persona=='user') before any character/narrator turns.
    History is already in chronological order (root→leaf) via
    `path_to_root`, so we iterate in reverse.

    Returns False when there's no focal, no history, no recent user
    message, or the recent user message isn't a text addressed to
    THIS focal.
    """
    focal_id = ctx.focal_id
    if not focal_id:
        return False
    for msg in reversed(ctx.history or []):
        if msg.get("persona") != "user":
            continue
        meta = (msg.get("metadata") or {}).get("modules") or {}
        texting_meta = meta.get(MODULE_ID) or {}
        target = texting_meta.get("to")
        return isinstance(target, str) and target == focal_id
    return False


@register_filter
def _texting_strip_environmental(entry, block, ctx) -> bool:
    """Drop environmental blocks when the focal is replying to a text.

    Filter returns False to drop. Runs for every block the assembler
    emits; fast-paths to True (keep) when texting isn't active for
    this conversation, when the block isn't environmental, or when
    the user message we're replying to wasn't addressed at the focal.
    """
    if "environmental" not in (entry.tags or ()):
        return True  # not our concern
    if not _is_active(ctx.conversation, MODULE_ID, ctx.leaf_id):
        return True  # module isn't on for this branch
    if not _focal_is_replying_to_text(ctx):
        return True  # not a text reply
    setting_key = _BLOCK_TO_SETTING.get(entry.id)
    if not setting_key:
        # Environmental block we don't have a per-setting toggle
        # for. Default to keeping it — author can add a toggle later
        # if they want the strip.
        return True
    settings = _settings(ctx.conversation, ctx.leaf_id)
    # Setting True = drop the block; False = keep.
    return not settings.get(setting_key, True)


@register(id="texting_phone_primer", order=55, applies_to=("character",))
def _texting_phone_primer(ctx):
    """Prime the reply, on a text turn, to open with the character
    pulling out / checking their phone — grounding the SMS exchange as
    a physical act rather than a disembodied chat line.

    Fires ONLY when the texting module is active and the focal is
    replying to a text addressed at them (same gate as the environmental
    strip), so ordinary in-person turns are untouched. Phrased to skip
    the re-pull mid-exchange ('unless she already has it in hand'), so a
    back-and-forth doesn't narrate her fishing the phone out every line.
    """
    if not _is_active(ctx.conversation, MODULE_ID, ctx.leaf_id):
        return None
    if not _focal_is_replying_to_text(ctx):
        return None
    from app.personas import apply_macros

    content = (
        "{{char}} is not in the room with {{user}} — this is a text message. "
        "{{char}} is wherever she happens to be, and her phone has just buzzed "
        "with {{user}}'s text. Open the reply with her noticing and pulling out "
        "/ picking up her phone and reading it — unless she already has it in "
        "hand from an ongoing back-and-forth — then thumbing out her response. "
        "Keep the *action* beats on her and her phone; she can't see {{user}}'s "
        "surroundings.\n"
        "\n"
        "Example:\n"
        "{{user}}: hey, you up?\n"
        "{{char}}: *her phone buzzes face-down on the nightstand; she reaches "
        "over and squints at the bright screen in the dark, thumbing out a "
        "reply* \"mm... barely. what's up?\""
    )
    return Block(
        label="Texting",
        content=apply_macros(content, ctx.macros),
        section="Texting",
    )


@register_message_annotator
def _annotate_text_reply(conversation, message):
    """Persist the SMS marker onto a character reply that's answering a
    text, so the texting render style re-applies on reload instead of
    being re-derived client-side from a fragile immediate-parent walk.

    A reply counts as a text iff the NEAREST user message in its
    ancestry is a text addressed at this reply's speaker — mirroring the
    prompt-side `_focal_is_replying_to_text`. The marker keys off that
    persisted user-message metadata, so it self-gates: no user-side text
    marker, no annotation. Branch-specific by construction — it lives on
    this node, on this path, exactly like `applied_edits`.
    """
    persona = message.get("persona")
    if persona in (None, "user", "narrator"):
        return None
    speaker_id = message.get("speaker_id") or persona
    if not isinstance(speaker_id, str):
        return None
    msgs = conversation.get("messages") or {}
    # Skip this message itself if it's somehow already marked.
    own = ((message.get("metadata") or {}).get("modules") or {}).get(MODULE_ID)
    if isinstance(own, dict) and own:
        return None
    cur = msgs.get(message.get("parent_id"))
    seen = 0
    while cur is not None and seen < 200:  # cycle/runaway guard
        seen += 1
        if cur.get("persona") == "user":
            tmeta = ((cur.get("metadata") or {}).get("modules") or {}).get(MODULE_ID) or {}
            target = tmeta.get("to")
            if isinstance(target, str) and target == speaker_id:
                return {"modules": {MODULE_ID: {"to": speaker_id, "reply": True}}}
            # Nearest user message isn't a text to me — not a text reply.
            return None
        cur = msgs.get(cur.get("parent_id"))
    return None
