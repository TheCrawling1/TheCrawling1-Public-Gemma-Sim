"""Narrator-driven edit of an existing message.

The user selects a message in the chat and types a directive describing
what should be different — usually a state change like an outfit swap,
a move, or a mood shift. The narrator gets a focused prompt with:

  - The cast (character ids and names)
  - Available outfits per character (so it can't invent ids)
  - A worked example of the expected output format
  - The target message and the user directive

It returns:
  1. Edit directives ([outfit ...], [move ...], [set ...]) — the
     existing extract_edits() infrastructure pulls these out.
  2. A rewritten message body — same speaker, same general beat,
     similar length, with physical details updated.

The caller (route or test) takes the result, applies the edits to the
instance, replaces the target message body, updates the message's
presence_snapshot, and stores the directive + raw response in
metadata.narrator_edit for later UI display.

This module just runs the model and parses the output. State changes
and persistence are the caller's responsibility so the SSE-streaming
route and the offline test can share the same core.
"""
from __future__ import annotations

from typing import Any

from . import conversations as _convs
from .entities import load_instance_entities
from .narrator import extract_edits
from .narrator_apply import apply_edits
from .ollama_client import chat_stream, chat_sync
from .personas import _style_discipline_block, banned_phrase_hits, compose_wardrobe_extra


# An explicit, edit-mode-only system prompt. Doesn't reuse the standard
# narrator system block — the conversation-history primer and full
# character cards bias the model toward "continue the scene" rather
# than "rewrite this specific message". This prompt is tighter and
# tells the model exactly what to do.
EDIT_SYSTEM_TEMPLATE = """\
You are editing a single message in an interactive roleplay scene. The user selected a specific message and gave a directive describing what should be different. Your job is to update the scene state and rewrite the message.

Output format (exactly this — directives first, blank line, then the rewritten body):

[outfit <character_id> -> <outfit_id>]
[equip <character_id>.<slot> = <piece_id>]
[unequip <character_id>.<slot>]
[move <character_id> -> <room_id>]
[set <entity_id>.<dotted.path> = <value>]

<the rewritten message body>

Rules:
1. Emit edit directives FIRST, each on its own line, before any prose. The grammar is fixed — match it exactly.
2. Use only the character_ids, outfit_ids, and room_ids listed under [Available data] below. Do not invent ids.
3. After the directives, leave one blank line, then write the rewritten message body. Same speaker. Same general beat and length. Adjust the physical / sensory details to reflect the directive.
4. Do not explain what you are changing. Do not summarize. Do not add commentary like "Here is the rewritten version:". Just emit the directives and the rewritten body.
5. `[set <character>.<key> = <value>]` records persistent state on a character that future turns will see. Use it for status the model should keep tracking — power level, mood flag, item in their hand, an injury, a temporary effect — anything not already covered by [outfit] / [move]. Snake_case keys; scalar values (number, string, "true"/"false", or a JSON-quoted string). Example: `[set iris.power_level = "low"]`. Anything you set this way surfaces to the character on their next turn under "Current state".
6. Clothing: `[outfit ... -> ...]` swaps the WHOLE outfit. To change ONE garment and keep the rest, use `[equip <char>.<slot> = <piece_id>]` (optionally `state=<name>`), `[unequip <char>.<slot>]`, or to tweak an already-worn piece `[set <char>.properties.worn.<slot>.state = <state_name>]`. Use only the piece ids and state names listed under [Available clothing] below (present when a slot-based character is in scene).

[Available data]

Characters:
{cast}

Outfits per character:
{outfit_roster}

Rooms:
{rooms}

[Worked example]

Target message (speaker=Narrator): The morning sun fills the kitchen as Alice pours her coffee, still in the soft white pyjamas she slept in.
Directive: Swap Alice to her work suit and have her in the office.

Expected output:
[outfit alice -> alice_business_suit]
[move alice -> office_floor]

The fluorescent overheads of the office hum as Alice stands at her desk, sleeves of her tailored work suit pushed to the elbows, coffee in hand. The morning light through the tall windows catches the navy fabric.
{wardrobe_extra}
[Target message — speaker={speaker}]
{body}

[User directive]
{directive}

Now produce your output:"""


def _speaker_label_for(message: dict[str, Any], entities: dict[str, Any]) -> str:
    persona = message.get("persona")
    speaker = message.get("speaker_id") or persona
    if persona == "narrator":
        return "Narrator"
    if persona == "user":
        return "User"
    if speaker and speaker in entities:
        return entities[speaker].get("name") or speaker
    return speaker or persona or "?"


def _build_world_summary(
    entities: dict[str, dict[str, Any]],
    presence: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str, str]:
    """Return (cast_lines, outfit_roster_lines, rooms_lines) — formatted
    blocks listing the ids the narrator may use.

    When `presence` (the target message's per-character
    `{char_id: {room, location, ...}}` snapshot) is given, each cast line
    is annotated with the character's CURRENT room. Without it the roster
    is a flat id list and the model can't tell who is already here vs. in
    another room — so a directive like "Milo comes in from the
    back office" gets narrated in prose but no `[move]` is emitted.

    The outfit roster lists each character's owned outfits AND a
    "Generic (any character can wear)" subsection that pulls in any
    template-library outfit that lacks an `owner` / `worn_by` field
    (e.g. `bikini_generic`, `thin_shirt_generic`). Those ownerless
    outfits are auto-instanced into the conversation on first use by
    `narrator_apply.apply_edits` (the [outfit] handler copies the
    template into the instance dir if it isn't already there), so the
    narrator can put any character in any generic outfit by id.
    """
    chars = [e for e in entities.values() if e.get("type") == "character"]
    rooms = [e for e in entities.values() if e.get("type") == "room"]
    presence = presence or {}

    cast_lines: list[str] = []
    for c in sorted(chars, key=lambda e: e.get("id") or ""):
        cid = c.get("id") or ""
        name = c.get("name") or cid
        loc = presence.get(cid) or {}
        room = loc.get("room")
        where = f"  — in {room}" if room else "  — (not placed in a room)"
        cast_lines.append(f"  - {cid}  ({name}){where}")
    cast = "\n".join(cast_lines) if cast_lines else "  (none)"

    roster_lines: list[str] = []
    for c in sorted(chars, key=lambda e: e.get("id") or ""):
        cid = c.get("id") or ""
        props = c.get("properties") or {}
        outfits = list(props.get("outfits") or [])
        current = props.get("current_outfit")
        if current and current not in outfits:
            outfits.insert(0, current)
        if not outfits:
            roster_lines.append(f"  - {cid}: (no outfits registered)")
            continue
        marked = []
        for o in outfits:
            ent = entities.get(o)
            label = (ent.get("name") if ent else o) or o
            tag = "  *current*" if o == current else ""
            marked.append(f"      {o}  ({label}){tag}")
        roster_lines.append(f"  - {cid}:\n" + "\n".join(marked))

    # Generic outfit pool: anything in the template library with no
    # `owner` / `worn_by` is fair game for any character. Skip the
    # current entities map (which is conversation-instance only) and
    # walk the global template library so outfits that haven't been
    # pulled into the instance yet still surface.
    try:
        from . import entities as _ent
        all_outfits = _ent.by_type("outfit")
    except Exception:
        all_outfits = []
    generic_lines: list[str] = []
    for o in sorted(all_outfits, key=lambda e: e.get("id") or ""):
        oid = o.get("id") or ""
        if not oid:
            continue
        oprops = o.get("properties") or {}
        owner = oprops.get("owner")
        worn_by = oprops.get("worn_by")
        # An outfit counts as generic if it has no owner / worn_by, or
        # explicitly marks them as the literal string "generic".
        if owner and owner != "generic":
            continue
        if worn_by and worn_by != "generic":
            continue
        # Hide files that exist purely as `extends` bases — their
        # display name ends with "(base)" by convention. The narrator
        # shouldn't dress a character in just the base layer.
        name = (o.get("name") or oid).strip()
        if name.endswith("(base)"):
            continue
        generic_lines.append(f"      {oid}  ({name})")
    if generic_lines:
        roster_lines.append("  - GENERIC (any character can wear):\n" + "\n".join(generic_lines))

    roster = "\n".join(roster_lines) if roster_lines else "  (none)"

    rooms_lines: list[str] = []
    for r in sorted(rooms, key=lambda e: e.get("id") or ""):
        rid = r.get("id") or ""
        name = r.get("name") or rid
        rooms_lines.append(f"  - {rid}  ({name})")
    rooms_text = "\n".join(rooms_lines) if rooms_lines else "  (none)"

    return cast, roster, rooms_text


def _compose_edit_system(
    conversation: dict[str, Any],
    target_mid: str,
    directive: str,
) -> str:
    """Compose the narrator-edit instruction string (template +
    interpolated world data), WITHOUT the trailing style-discipline
    block — that is composed by the registry's `style_discipline` block
    (order 190) so it stays a single source of truth across every
    prompt. The registry block `narrator_edit_instructions` wraps this."""
    if target_mid not in conversation["messages"]:
        raise ValueError(f"Target message {target_mid!r} not found in conversation.")
    target = conversation["messages"][target_mid]

    # Branch-scoped entities — see narrator_add.build_add_prompt for
    # the same fix + rationale.
    from .effective import effective_entities_at, branch_filter
    entities = effective_entities_at(conversation, target_mid)
    entities = branch_filter(conversation, target_mid, entities)
    presence = (target.get("presence_snapshot") or {}).get("presence") or {}
    cast, outfit_roster, rooms = _build_world_summary(entities, presence)
    speaker = _speaker_label_for(target, entities)
    body = (target.get("content") or "").strip()

    # Wardrobe instructions — v2 slot-based grammar for characters with a
    # `worn` map, plus the legacy v1 clothing_overrides grammar for any
    # sprite-rendered character. Each block names the characters it
    # applies to; a mixed scene gets both. Empty when neither applies, so
    # non-wardrobe scenes stay lean.
    wardrobe_extra = compose_wardrobe_extra(entities)

    system = EDIT_SYSTEM_TEMPLATE.format(
        cast=cast,
        outfit_roster=outfit_roster,
        rooms=rooms,
        speaker=speaker,
        body=body,
        directive=directive.strip(),
        wardrobe_extra=wardrobe_extra,
    )
    return system


# Register the narrator-edit instruction body as a prompt-registry block
# under the "narrator_edit" persona. The registry's shared
# `style_discipline` block (order 190, applies_to includes
# "narrator_edit") appends the banned-phrase guidance after it — same
# output as the old `f"{system}\n\n{sd}"`, now single-sourced.
from .prompt import Block as _Block, register as _register  # noqa: E402


@_register(id="narrator_edit_instructions", order=10, applies_to=("narrator_edit",))
def _block_narrator_edit(ctx):
    aux = ctx.settings.get("_aux") or {}
    target_mid = aux.get("target_mid")
    directive = aux.get("directive") or ""
    if not target_mid:
        return None
    return _Block(
        label="Narrator-edit instructions",
        content=_compose_edit_system(ctx.conversation, target_mid, directive),
        section=None,
    )


def build_edit_prompt(
    conversation: dict[str, Any],
    target_mid: str,
    directive: str,
) -> dict[str, Any]:
    """Build the prompt for narrator-edit. Returns the same shape as
    `assemble_prompt` (system + messages + stop). Assembled through the
    prompt registry (`narrator_edit` persona)."""
    from .prompt import PromptContext, assemble, build_context
    ctx: PromptContext = build_context(
        conversation, persona="narrator_edit", leaf_id=target_mid,
    )
    ctx.settings["_aux"] = {"target_mid": target_mid, "directive": directive}
    return {
        "system": assemble(ctx).system,
        "messages": [],
        "stop": [],
        "pieces": [],
    }


def narrator_edit_message_sync(
    conversation: dict[str, Any],
    target_mid: str,
    directive: str,
    *,
    model: str | None = None,
    options: dict[str, Any] | None = None,
    think: bool = False,
) -> dict[str, Any]:
    """Run the narrator on (target message, directive) and return the
    parsed result. Synchronous — drives chat_stream and accumulates both
    the content and (if `think=True`) the thinking trace.

    Returns:
      {
        "new_body": str,            # the rewritten message body
        "edits": list[dict],        # extracted edit directives
        "raw_response": str,        # the model's content output
        "thinking_trace": str,      # reasoning trace (empty if think=False
                                    # or model didn't engage)
        "directive": str,           # echoed back
        "target_mid": str,          # echoed back
      }
    """
    prompt = build_edit_prompt(conversation, target_mid, directive)
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    for ev in chat_stream(
        system=prompt["system"],
        messages=prompt["messages"],
        model=model,
        options=options,
        think=think,
    ):
        if ev["kind"] == "thinking":
            thinking_parts.append(ev["text"])
        else:
            content_parts.append(ev["text"])
    response = "".join(content_parts)
    thinking_trace = "".join(thinking_parts)
    new_body, edits = extract_edits(response)
    return {
        "new_body": new_body.strip(),
        "edits": edits,
        "raw_response": response,
        "thinking_trace": thinking_trace,
        "directive": directive,
        "target_mid": target_mid,
    }


def narrator_edit_message_stream(
    conversation: dict[str, Any],
    target_mid: str,
    directive: str,
    *,
    model: str | None = None,
    options: dict[str, Any] | None = None,
    think: bool = False,
):
    """Streaming version — yields {"kind", "text"} dicts as chat_stream does.
    Caller accumulates and parses."""
    prompt = build_edit_prompt(conversation, target_mid, directive)
    yield from chat_stream(
        system=prompt["system"],
        messages=prompt["messages"],
        model=model,
        options=options,
        think=think,
    )


def _resolve_narrator_model(conv: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Resolve (model, sampling) the same way the narrator routes do, for
    the pairs second-call. Needs a Flask app context (routes + tests have
    one)."""
    from flask import current_app
    settings = conv.get("settings") or {}
    model = settings.get("ollama_model_override") or (
        current_app.config.get("ollama") or {}
    ).get("model")
    profile = (current_app.config.get("model_profiles") or {}).get(model) or {}
    sampling = {**profile, **(settings.get("sampling") or {})}
    return model, sampling


def _changed_character_ids(edits: list[dict[str, Any]]) -> list[str]:
    """Character ids whose IDENTITY/APPEARANCE meaningfully changed — a
    patch touching description / body_parts / personality, or a derive
    (`_materialize_from`). These warrant a dialogue-pairs rewrite; a bare
    move/outfit/notes edit does not. Tolerant of the misplaced-`properties`
    form (keys may sit at the top level of `data`)."""
    out: list[str] = []
    for e in edits or []:
        if not isinstance(e, dict) or e.get("kind") != "patch":
            continue
        eid = e.get("id")
        if not eid or eid == "user" or eid in out:
            continue
        data = e.get("data") or {}
        props = data.get("properties") or {}
        keys = set(data.keys()) | (set(props.keys()) if isinstance(props, dict) else set())
        if keys & {"description", "body_parts", "personality", "_materialize_from"}:
            out.append(eid)
    return out


def append_narrator_edit_result(
    conv: dict[str, Any],
    target_mid: str,
    *,
    directive: str,
    raw_response: str,
    new_body: str,
    edits: list[dict[str, Any]],
    thinking_text: str = "",
    reason: str = "complete",
) -> dict[str, Any]:
    """Persist a narrator-edit result as a new sibling of `target_mid`.

    Mirrors what the streaming `/narrator-edit/<mid>` endpoint does on
    a successful completion — applies the parsed edits, builds a new
    presence_snapshot, writes both `metadata.applied_edits` (read by
    path-replay / picker) and `metadata.narrator_edit` (read by the
    UI for narrator-edit-specific affordances), then saves. Returns
    the new message dict.

    Extracted into a helper so the test can drive the same persistence
    code production uses; previously the test fabricated metadata in
    a more permissive shape (edits in both `applied_edits` and
    `narrator_edit.applied`) which masked the bug where stream.py was
    only writing the latter.
    """
    target_live = (conv.get("messages") or {}).get(target_mid)
    if target_live is None:
        raise ValueError(f"target message {target_mid!r} not in conversation")

    parent_id = target_live.get("parent_id")
    if parent_id:
        parent_msg = (conv.get("messages") or {}).get(parent_id) or {}
        parent_snap = parent_msg.get("presence_snapshot") or {}
    else:
        # Root edits append a new root; use the target's own snapshot
        # since there's no parent to inherit from.
        parent_snap = target_live.get("presence_snapshot") or {}

    settings = conv.get("settings") or {}
    user_persona = settings.setdefault(
        "user_persona", {"name": "User", "description": ""}
    )
    # Path-cast gating: skip redundant paired cast_adds for chars
    # already on the parent path's effective cast. A genuinely-new
    # narrator-instanced char still gets its cast_add (this is the
    # first time the id appears on the path, so it's not in the set).
    from .effective import effective_cast_at as _ec
    existing_cast = _ec(conv, parent_id).get("characters") or set() if parent_id else set()
    presence_patch, applied_log = apply_edits(
        conv["id"], edits, parent_snap, user_persona=user_persona,
        existing_cast_chars=existing_cast,
    )

    new_snap = dict(parent_snap)
    presence = dict((parent_snap.get("presence") or {}))
    for char_id, patch in presence_patch.items():
        prev = dict(presence.get(char_id) or {})
        prev.update({k: v for k, v in patch.items() if v})
        presence[char_id] = prev
    new_snap["presence"] = presence

    meta: dict[str, Any] = {
        "narrator_edit": {
            "directive": directive,
            "raw_response": raw_response,
            "thinking_trace": thinking_text,
            "edits": edits,
            "applied": applied_log,
            "edited_from": target_mid,
            "reason": reason,
        }
    }
    # Path-replay (effective._path_applied_edits) only walks
    # `metadata.applied_edits`. Without this mirror, any [set ...]
    # edit a narrator-edit emits — including [set <char>.properties.
    # clothing_overrides.<slot> = ...] — is invisible to the picker
    # / effective-state readers.
    if applied_log:
        meta["applied_edits"] = applied_log
    phrase_hits = banned_phrase_hits(new_body)
    if phrase_hits:
        meta["phrase_hits"] = phrase_hits

    new_msg = _convs.append_message(
        conv,
        parent_id=parent_id,
        persona=target_live.get("persona") or "narrator",
        content=new_body or target_live.get("content") or "",
        speaker_id=target_live.get("speaker_id"),
        presence_snapshot=new_snap,
        metadata=meta,
    )

    # Pairs second-call — gated by narrator_controls.edit_pairs. After the
    # main edits apply, rewrite the dialogue examples of any character whose
    # identity/appearance meaningfully changed, so their voice matches what
    # they've become (a "she can't talk" doll gets silent example pairs).
    # Reads the character's EFFECTIVE state at the new leaf (so it sees the
    # just-applied transformation) and appends its patch to this message's
    # applied_edits. Best-effort: a failure here never breaks the edit.
    try:
        from . import narrator_pairs
        if narrator_pairs.is_active(conv):
            changed = _changed_character_ids(edits)
            if changed:
                from .effective import effective_entities_at
                model, options = _resolve_narrator_model(conv)
                eff = effective_entities_at(conv, new_msg["id"])
                pair_applied: list[dict[str, Any]] = []
                for char_id in changed:
                    char = eff.get(char_id)
                    if not isinstance(char, dict) or char.get("type") != "character":
                        continue
                    pr = narrator_pairs.rewrite_pairs_sync(
                        char=char, speaker_id=char_id, model=model, options=options,
                    )
                    pedit = pr.get("edit")
                    if not pedit:
                        continue
                    _pp, applied2 = apply_edits(
                        conv["id"], [pedit], parent_snap,
                        user_persona=user_persona, existing_cast_chars=existing_cast,
                    )
                    pair_applied.extend(applied2)
                if pair_applied:
                    md = new_msg.setdefault("metadata", {})
                    md["applied_edits"] = (md.get("applied_edits") or []) + pair_applied
                    md["narrator_pairs"] = {"applied": pair_applied}
    except Exception:
        pass

    _convs.save_conversation(conv)
    return new_msg
