"""Auto state-changes side call.

After a character turn streams to completion, this module runs a second,
surgical model call that re-reads the prose just produced and emits any
``[set <char>.properties.clothing_overrides.<slot> = <state>]`` /
``[outfit <char> -> <id>]`` directives the main generation forgot. The
goal is to keep the rendered image in sync with what the prose actually
described — the failure mode where a reply says "her jacket is off"
but the picker still sees top=1.

Design notes:

  - Side-call shape is the same as ``narrator_edit`` and the catalog
    image-pick: a focused system prompt, no chat history, parses the
    model's output through the existing ``extract_edits()``.
  - Output is directives only — empty output is the success case for
    messages that don't change anything.
  - Branch isolation comes for free: the caller patches the resulting
    edits onto the just-generated message's ``metadata.applied_edits``,
    which path-replay walks the same way as inline-emitted edits.
  - Two cost gates live at the call site, not here: per-conv toggle
    (default off) and skip when the main-gen already emitted clothing
    edits for the focal speaker.
  - The pass runs on a separate POST endpoint
    (``/messages/<mid>/auto_state``) that the browser fires AFTER the
    SSE stream closes, NOT inline in the streaming finalisation.
    Putting a second model round-trip on the SSE response held it open
    for the duration of the side call, which the user perceives as a
    hang. Same architectural shape the catalog ``image_pack_pick``
    uses for the same reason.
  - Uses the SAME model the conversation's main generate uses (mirroring
    ``narrator_edit``). No separate model knob — the keep-alive-pinned
    main model is already resident in VRAM, so this is a fast warm
    round-trip. Configuring a different model would force VRAM
    swap-out/reload on every alternation between main-gen and the side
    pass, defeating the point.
"""
from __future__ import annotations

from typing import Any

from .effective import effective_cast_at, effective_entities_at
from .narrator import extract_edits
from .ollama_client import chat_sync


_AUTO_STATE_SYSTEM_TEMPLATE = """\
You are watching for wardrobe state changes in roleplay prose so the character image renderer stays in sync with what the text describes.

You are given:
  - The focal character (name + id)
  - Their current clothing state (which slots are on / partial / off, plus the outfit they're wearing)
  - The list of outfits they have available
  - The message they just produced

Your job: emit directives ONLY if the message describes a wardrobe change that ISN'T already reflected in the current state.

Output format — directives only, one per line, NO commentary, NO prose:

  [set <character_id>.properties.clothing_overrides.<slot> = <state>]
  [outfit <character_id> -> <outfit_id>]

Slot names: top, bottom, bra, underwear, pantyhose, gloves, legwear, shoes
States: 1 = worn normally, 2 = displaced / partial / rolled-up, 3 = removed / off

Rules:
1. If the message describes a wardrobe CHANGE — a piece coming off, a strap unhooked, a button popping, a scarf slipping loose, a jacket coming off, a swap to a different outfit — emit the matching directive at the top.
2. If the message merely describes current state without changing it (e.g. "her feet are bare" when shoes are already 3 / off), emit NOTHING. Empty output is the SUCCESS case for messages that don't change anything.
3. If the message is ambiguous or you're not sure whether something changed, err on the side of emitting nothing.
4. Only emit directives for the focal character below. Never fire for other characters mentioned in the message.
5. Use only the outfit_ids listed under [Available outfits]. Do not invent ids.
6. Use only the eight slot names above. Do not invent fields like `wearing_bra` or `shirt_status` — only `clothing_overrides.<slot>` is read by the renderer.
7. If the character currently has no outfit on (current outfit named `nude` or `nude_female_generic`, or every slot already OFF), do NOT emit `[set ...clothing_overrides.<slot> = 1]` patches even if the prose hints at fabric. Going from undressed back to dressed requires the narrator to swap the outfit explicitly (`[outfit <id> -> <outfit_id>]`); slot overrides on top of an empty outfit don't render coherently — the outfit's coverage stays off, while the override claims a garment exists. If the directive really intended re-dressing, emit ONLY an outfit swap; otherwise emit nothing.

[Worked example — change happened]

Character: Nadia (id: nadia)
Currently wearing: nadia_work — her shop outfit; cardigan, white shirt, dark skirt, canvas shoes
Current per-slot state: top: ON · bottom: ON · bra: ON · underwear: ON · pantyhose: OFF · gloves: OFF · legwear: ON · shoes: ON
Available outfits: nadia_work, nadia_shirt_up, nadia_thin_shirt, nadia_casual, nadia_short_skirt
Message just generated:
*She reaches back, unhooks her bra and slides it out from under her shirt, dropping it onto the counter between you.* "There. Comfier."

Expected output:
[set nadia.properties.clothing_overrides.bra = 3]

[Worked example — nothing changed]

Character: Nadia (id: nadia)
Currently wearing: nadia_thin_shirt — thin white t-shirt, dark skirt, no bra
Current per-slot state: top: ON · bottom: ON · bra: OFF · underwear: ON · pantyhose: OFF · gloves: OFF · legwear: ON · shoes: ON
Available outfits: nadia_work, nadia_shirt_up, nadia_thin_shirt, nadia_casual, nadia_short_skirt
Message just generated:
*She turns toward you, arms folded, the thin cotton shirt hanging loose.* "Don't say anything."

Expected output:


[Worked example — character is already changed down, prose stays in the back office]

Character: Nadia (id: nadia)
Currently wearing: nude_female_generic — no clothing on any part of her
Current per-slot state: top: OFF · bottom: OFF · bra: OFF · underwear: OFF · pantyhose: OFF · gloves: OFF · legwear: OFF · shoes: OFF
Available outfits: nadia_work, nadia_shirt_up, nadia_thin_shirt, nadia_casual, nadia_short_skirt
Message just generated:
*She hugs her arms around herself against the draught, hair still dripping from the downpour outside.* "Mm."

Expected output:


(Nothing — she had no outfit on, and she still has none. Slot=1 patches would re-dress her on top of an empty outfit and the renderer can't honor that. If she had put a towel on, the right output would be an outfit swap to a towel outfit, NOT slot overrides.)


[Now your turn]

Character: {char_name} (id: {char_id})
Currently wearing: {outfit_summary}
Current per-slot state: {slot_summary}
Available outfits: {outfit_roster}

Message just generated:
{message_body}

Now produce your output (directives only — empty if nothing changed):"""


_SLOT_NAMES = ("top", "bottom", "bra", "underwear", "pantyhose", "gloves", "legwear", "shoes")
_STATE_LABEL = {1: "ON", 2: "PARTIAL", 3: "OFF"}


def has_clothing_edit_for(
    edits: list[dict[str, Any]] | None, char_id: str
) -> bool:
    """Return True if `edits` already carries an explicit per-slot
    ``clothing_overrides`` patch for `char_id` — the only kind of edit
    that fully precludes an auto-state pass.

    Outfit swaps are deliberately NOT a skip trigger. The original
    failure mode (Rosa conv ``b7070f76daaf``) was: main-gen emitted
    ``[outfit rosa -> rosa_raincoat]`` and then later prose
    drifted (a button described as undone) without an accompanying
    ``clothing_overrides`` patch. Treating the outfit swap as "main-gen
    handled clothing" would skip exactly the cases auto-state was
    designed to catch. Outfit swaps set the *baseline*; per-slot
    overrides on top of that baseline are what we look for.
    """
    if not edits or not char_id:
        return False
    for e in edits:
        if e.get("kind") != "patch" or e.get("id") != char_id:
            continue
        data = e.get("data") or {}
        if not isinstance(data, dict):
            continue
        overrides = (data.get("properties") or {}).get("clothing_overrides")
        if isinstance(overrides, dict) and overrides:
            return True
    return False


def build_prompt(
    *,
    conversation: dict[str, Any],
    speaker_id: str,
    message_body: str,
    leaf_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the auto-state side-call prompt.

    Resolves the character's current outfit, clothing_slots, and
    clothing_overrides via ``effective_entities_at`` so the model sees
    the same state the renderer would render. ``leaf_id`` defaults to
    the conversation's active leaf — passing the just-generated message
    id is the right thing during stream finalisation.
    """
    eff = effective_entities_at(conversation, leaf_id)
    char = eff.get(speaker_id) or {}
    char_name = char.get("name") or speaker_id
    props = char.get("properties") or {}

    outfit_id = props.get("current_outfit") or ""
    outfit = eff.get(outfit_id) if outfit_id else None
    outfit_props = (outfit or {}).get("properties") or {}

    if outfit:
        summary = (
            outfit_props.get("concise_description")
            or outfit.get("name")
            or outfit_props.get("intact_description")
            or outfit_id
        )
        outfit_summary = f"{outfit_id} — {summary}" if summary else outfit_id
    else:
        outfit_summary = "(no outfit set)"

    base_slots = dict(outfit_props.get("clothing_slots") or {})
    overrides = props.get("clothing_overrides") or {}
    if isinstance(overrides, dict):
        for slot, value in overrides.items():
            try:
                n = int(value)
            except (TypeError, ValueError):
                continue
            if n in (1, 2, 3) and isinstance(slot, str):
                base_slots[slot.lower()] = n
    slot_pairs: list[str] = []
    for name in _SLOT_NAMES:
        try:
            v = int(base_slots.get(name, 1))
        except (TypeError, ValueError):
            v = 1
        slot_pairs.append(f"{name}: {_STATE_LABEL.get(v, 'ON')}")
    slot_summary = " · ".join(slot_pairs)

    available = props.get("outfits") or []
    if isinstance(available, list) and available:
        outfit_roster = ", ".join(str(o) for o in available)
    else:
        outfit_roster = outfit_id or "(none)"

    system = _AUTO_STATE_SYSTEM_TEMPLATE.format(
        char_name=char_name,
        char_id=speaker_id,
        outfit_summary=outfit_summary,
        slot_summary=slot_summary,
        outfit_roster=outfit_roster,
        message_body=(message_body or "").strip() or "(empty message)",
    )
    return {"system": system, "messages": []}


def auto_state_changes_sync(
    *,
    conversation: dict[str, Any],
    speaker_id: str,
    message_body: str,
    leaf_id: str | None = None,
    model: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the side call and return parsed edits.

    Returns:
      {
        "edits":      list[dict],   # [set ...] / [outfit ...] only
        "raw":        str,          # the model's content output
        "prompt":     dict,         # the system prompt that was sent
      }

    Filters out any edits that don't target the focal speaker (defense
    against the model firing for the wrong character) and any non-
    clothing patch edits (defense against scope creep into general
    `[set ...]` directives — auto-state is wardrobe-only). Also
    drops slot=1 / slot=2 patches when the focal currently has no
    outfit — the prompt teaches the model to skip those (rule 7), this is the
    safety floor for when the model emits them anyway.
    """
    prompt = build_prompt(
        conversation=conversation,
        speaker_id=speaker_id,
        message_body=message_body,
        leaf_id=leaf_id,
    )
    try:
        raw = chat_sync(
            system=prompt["system"],
            messages=prompt["messages"],
            model=model,
            options=options or {},
            think=False,  # surgical directive emission, never reason
        )
    except Exception:
        return {"edits": [], "raw": "", "prompt": prompt}

    _, edits = extract_edits(raw)
    filtered = _filter_edits(
        edits, speaker_id,
        currently_nude=_is_currently_nude(
            conversation, speaker_id, leaf_id,
        ),
    )
    return {"edits": filtered, "raw": raw, "prompt": prompt}


def _is_currently_nude(
    conversation: dict[str, Any],
    speaker_id: str,
    leaf_id: str | None,
) -> bool:
    """True when the focal character's effective state at this leaf
    has no outfit on — either current_outfit's id contains 'nude', the
    outfit's tags include 'nude', or every clothing slot (after
    overrides) reads 3 (off). Used by `_filter_edits` to drop
    slot=1 / slot=2 patches that would re-dress on top of an empty
    outfit; the renderer can't honor those coherently."""
    try:
        eff = effective_entities_at(conversation, leaf_id)
    except Exception:
        return False
    char = eff.get(speaker_id) or {}
    props = char.get("properties") or {}
    outfit_id = props.get("current_outfit") or ""
    outfit = eff.get(outfit_id) if outfit_id else None
    if outfit:
        if "nude" in (outfit_id or "").lower():
            return True
        tags = outfit.get("tags") or []
        if isinstance(tags, list) and "nude" in tags:
            return True
    # Derive from slot state: every visible slot already OFF.
    base_slots = dict(((outfit or {}).get("properties") or {}).get("clothing_slots") or {})
    overrides = props.get("clothing_overrides") or {}
    if isinstance(overrides, dict):
        for slot, value in overrides.items():
            try:
                n = int(value)
            except (TypeError, ValueError):
                continue
            if n in (1, 2, 3):
                base_slots[slot.lower()] = n
    visible = ("top", "bottom", "bra", "underwear")
    return all(int(base_slots.get(s, 1)) == 3 for s in visible)


def run_and_apply(
    *,
    conversation: dict[str, Any],
    msg: dict[str, Any],
    speaker_id: str,
    main_gen_edits: list[dict[str, Any]] | None = None,
    model: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the auto-state side call against ``msg`` and apply any emitted
    edits onto it in place.

    Mutates ``msg`` (extends ``metadata.applied_edits``, updates
    ``presence_snapshot``, writes ``metadata.auto_state_changes``) and
    returns a summary dict for the caller to log / return to the
    client. Does NOT save the conversation — the caller is responsible
    for persistence so this helper composes cleanly with the route
    handler and the offline test alike.

    The caller has already decided to invoke the pass (gate checks live
    one level up); we just run the model and apply what comes back.
    Skipping when ``main_gen_edits`` already covers clothing for the
    focal speaker is enforced here as a defensive double-gate.

    Returns:
      {
        "ran":         bool,                # whether the model was called
        "skipped":     str | None,          # reason if ran=False
        "edits":       list[dict],          # applied directives
        "applied_log": list[dict],          # per-edit ok/before/after rows
        "raw":         str,                 # raw model output (for debug)
      }
    """
    from .narrator_apply import apply_edits  # avoid import cycle at module load

    if has_clothing_edit_for(main_gen_edits, speaker_id):
        return {
            "ran": False,
            "skipped": "main_gen_already_emitted",
            "edits": [],
            "applied_log": [],
            "raw": "",
        }

    cid = conversation["id"]
    result = auto_state_changes_sync(
        conversation=conversation,
        speaker_id=speaker_id,
        message_body=msg.get("content") or "",
        leaf_id=msg["id"],
        model=model,
        options=options,
    )
    edits = result.get("edits") or []
    raw = result.get("raw", "")
    if not edits:
        return {
            "ran": True,
            "skipped": None,
            "edits": [],
            "applied_log": [],
            "raw": raw,
        }

    parent_id = msg.get("parent_id") or ""
    parent_msg = (conversation.get("messages") or {}).get(parent_id) or {}
    parent_snap = msg.get("presence_snapshot") or parent_msg.get("presence_snapshot") or {}
    settings = conversation.get("settings") or {}
    user_persona = settings.setdefault(
        "user_persona", {"name": "User", "description": ""}
    )
    # Drop redundant paired cast_adds — auto-state patches a char
    # already on the path's cast, so the per-edit cast_add companions
    # would just clutter the applied-edits panel.
    existing_cast = effective_cast_at(conversation, parent_id).get("characters") or set()
    presence_patch, applied_log = apply_edits(
        cid, edits, parent_snap, user_persona=user_persona,
        existing_cast_chars=existing_cast,
    )
    if not applied_log:
        return {
            "ran": True,
            "skipped": "no_applied_log",
            "edits": edits,
            "applied_log": [],
            "raw": raw,
        }

    new_snap = dict(parent_snap)
    presence = dict((parent_snap.get("presence") or {}))
    for char_id, patch in presence_patch.items():
        prev = dict(presence.get(char_id) or {})
        prev.update({k: v for k, v in patch.items() if v})
        presence[char_id] = prev
    new_snap["presence"] = presence

    msg["presence_snapshot"] = new_snap
    msg.setdefault("metadata", {}).setdefault("applied_edits", []).extend(applied_log)
    msg["metadata"]["auto_state_changes"] = {
        "edits": edits,
        "raw_response": raw,
    }
    return {
        "ran": True,
        "skipped": None,
        "edits": edits,
        "applied_log": applied_log,
        "raw": raw,
    }


def _filter_edits(
    edits: list[dict[str, Any]], speaker_id: str,
    *,
    currently_nude: bool = False,
) -> list[dict[str, Any]]:
    """Filter the model's raw edits to those targeting the focal
    speaker AND restricted to clothing_overrides patches + outfit
    swaps. When `currently_nude` is True (the character has no
    outfit on — see `_is_currently_nude`), additionally drop
    slot=1 / slot=2 overrides; those would re-dress on top of an
    empty outfit and the renderer can't honor them coherently. Only
    slot=3 (still-off) overrides and outfit swaps pass through in
    the empty-wardrobe case."""
    out: list[dict[str, Any]] = []
    for e in edits or []:
        kind = e.get("kind")
        if kind == "outfit":
            if e.get("character_id") == speaker_id:
                out.append(e)
            continue
        if kind == "patch" and e.get("id") == speaker_id:
            data = e.get("data") or {}
            if not isinstance(data, dict):
                continue
            overrides = (data.get("properties") or {}).get("clothing_overrides")
            if isinstance(overrides, dict) and overrides:
                # Defense (rule 7): drop re-dress overrides on a
                # character with no outfit. Going undressed → dressed
                # requires an outfit swap, not slot overrides.
                if currently_nude:
                    overrides = {
                        slot: val for slot, val in overrides.items()
                        if not (isinstance(val, int) and val in (1, 2))
                        and not (isinstance(val, str) and val.strip() in ("1", "2"))
                    }
                    if not overrides:
                        continue
                # Only keep the clothing_overrides nesting; drop any
                # other `properties.X` the model may have included.
                out.append({
                    "kind": "patch",
                    "id": speaker_id,
                    "data": {"properties": {"clothing_overrides": dict(overrides)}},
                })
    return out


# ---------------------------------------------------------------------------
# Extra auto-state passes (transparency, location). Same side-call recipe
# as the clothing pass above; each is gated by its own panel toggle and
# run by the /auto_state route after the clothing pass. New passes slot in
# by adding a {build, filter, meta_key} entry to _EXTRA_PASSES below.
# ---------------------------------------------------------------------------


_TRANSPARENCY_TEMPLATE = """\
You are watching for clothing-transparency changes in roleplay prose so the character renderer stays in sync with what the text describes.

You are given the focal character, the outfit they're wearing, their CURRENT per-slot transparency (0 = opaque, 100 = fully see-through), and the message they just produced.

Emit directives ONLY if the message describes a garment becoming MORE or LESS see-through than its current value — fabric going wet / soaked / sheer / translucent / sweat-soaked / oiled / stretched-thin, or drying back to opaque.

Output format — directives only, one per line, NO commentary:

  [set {char_id}.properties.clothing_transparency.<slot> = <0..100>]

Slots: top, bottom, bra, underwear, pantyhose, gloves, legwear, shoes
Value: 0 = opaque, ~40 = faintly visible through, ~70 = clearly see-through, 100 = effectively transparent.

Rules:
1. Only emit on a CHANGE from the current value. If nothing changed, emit NOTHING (empty output is the success case).
2. Only the focal character ({char_id}); only the clothing_transparency field; only the eight slots above.
3. A garment that is REMOVED is not "transparent" — leave that to the wardrobe pass.

[Now your turn]
Character: {char_name} (id: {char_id})
Outfit: {outfit_summary}
Current transparency: {transparency_summary}
Message just generated:
{message_body}

Output (directives only — empty if nothing changed):"""


_LOCATION_TEMPLATE = """\
You are watching for character MOVEMENT in roleplay prose so the scene's location state stays in sync with what the text describes.

You are given the focal character, their current room, the rooms available in the scene, and the message they just produced.

Emit a directive ONLY if the message clearly moves the focal character into a DIFFERENT room than their current one (walking into the kitchen, stepping out onto the balcony, heading to the bedroom).

Output format — directives only, one per line, NO commentary:

  [move {char_id} -> <room_id>]

Rules:
1. Only emit on a CLEAR room change. If the focal stays put, or movement is vague/incomplete, emit NOTHING (empty output is the success case).
2. Use ONLY a room_id from the [Available rooms] list. Do not invent ids.
3. Only the focal character ({char_id}).

[Now your turn]
Character: {char_name} (id: {char_id})
Current room: {current_room}
Available rooms:
{room_list}
Message just generated:
{message_body}

Output (directives only — empty if nothing changed):"""


def build_transparency_prompt(
    *, conversation: dict[str, Any], speaker_id: str,
    message_body: str, leaf_id: str | None = None,
) -> dict[str, Any]:
    eff = effective_entities_at(conversation, leaf_id)
    char = eff.get(speaker_id) or {}
    props = char.get("properties") or {}
    outfit_id = props.get("current_outfit") or ""
    outfit = eff.get(outfit_id) if outfit_id else None
    outfit_props = (outfit or {}).get("properties") or {}
    desc = outfit_props.get("concise_description") or (outfit or {}).get("name") or outfit_id
    outfit_summary = (f"{outfit_id} — {desc}" if (outfit and desc) else (outfit_id or "(none)"))
    tr = props.get("clothing_transparency") or {}
    pairs = []
    for s in _SLOT_NAMES:
        try:
            v = max(0, min(100, int(tr.get(s, 0))))
        except (TypeError, ValueError):
            v = 0
        pairs.append(f"{s}: {v}")
    system = _TRANSPARENCY_TEMPLATE.format(
        char_name=char.get("name") or speaker_id, char_id=speaker_id,
        outfit_summary=outfit_summary, transparency_summary=" · ".join(pairs),
        message_body=(message_body or "").strip() or "(empty message)",
    )
    return {"system": system, "messages": []}


def build_location_prompt(
    *, conversation: dict[str, Any], speaker_id: str,
    message_body: str, leaf_id: str | None = None,
) -> dict[str, Any]:
    eff = effective_entities_at(conversation, leaf_id)
    char = eff.get(speaker_id) or {}
    rooms = [e for e in eff.values() if e.get("type") == "room"]
    room_lines = [f"  - {r.get('id')} ({r.get('name') or r.get('id')})" for r in rooms if r.get("id")]
    # Current room from the leaf's presence snapshot.
    cur_room = ""
    msgs = conversation.get("messages") or {}
    leaf = msgs.get(leaf_id) or {}
    snap = leaf.get("presence_snapshot") or {}
    pres = (snap.get("presence") or {}).get(speaker_id) or {}
    cur_room = pres.get("room") or "(unknown)"
    system = _LOCATION_TEMPLATE.format(
        char_name=char.get("name") or speaker_id, char_id=speaker_id,
        current_room=cur_room,
        room_list="\n".join(room_lines) or "  (none)",
        message_body=(message_body or "").strip() or "(empty message)",
    )
    return {"system": system, "messages": []}


def _filter_transparency_edits(edits, speaker_id):
    out = []
    for e in edits or []:
        if not isinstance(e, dict) or e.get("kind") != "patch" or e.get("id") != speaker_id:
            continue
        ct = ((e.get("data") or {}).get("properties") or {}).get("clothing_transparency")
        if not isinstance(ct, dict) or not ct:
            continue
        clean = {}
        for slot, val in ct.items():
            if slot not in _SLOT_NAMES:
                continue
            try:
                clean[slot] = max(0, min(100, int(val)))
            except (TypeError, ValueError):
                continue
        if clean:
            out.append({"kind": "patch", "id": speaker_id,
                        "data": {"properties": {"clothing_transparency": clean}}})
    return out


def _filter_location_edits(edits, speaker_id):
    return [e for e in (edits or [])
            if isinstance(e, dict) and e.get("kind") == "move"
            and e.get("character_id") == speaker_id and e.get("room")]


# pass_id -> (prompt builder, edit filter, metadata key)
_EXTRA_PASSES = {
    "transparency": (build_transparency_prompt, _filter_transparency_edits, "auto_state_transparency"),
    "location": (build_location_prompt, _filter_location_edits, "auto_state_location"),
}


def _apply_pass_edits(conversation, msg, edits, raw, meta_key):
    """Shared apply tail: run edits through apply_edits, merge the
    presence patch into the message snapshot, stamp metadata. Mirrors
    run_and_apply's tail."""
    from .narrator_apply import apply_edits
    cid = conversation["id"]
    parent_id = msg.get("parent_id") or ""
    parent_msg = (conversation.get("messages") or {}).get(parent_id) or {}
    parent_snap = msg.get("presence_snapshot") or parent_msg.get("presence_snapshot") or {}
    settings = conversation.get("settings") or {}
    user_persona = settings.setdefault("user_persona", {"name": "User", "description": ""})
    existing_cast = effective_cast_at(conversation, parent_id).get("characters") or set()
    presence_patch, applied_log = apply_edits(
        cid, edits, parent_snap, user_persona=user_persona,
        existing_cast_chars=existing_cast,
    )
    if not applied_log:
        return {"ran": True, "skipped": "no_applied_log", "edits": edits, "applied_log": [], "raw": raw}
    new_snap = dict(parent_snap)
    presence = dict((parent_snap.get("presence") or {}))
    for char_id, patch in presence_patch.items():
        prev = dict(presence.get(char_id) or {})
        prev.update({k: v for k, v in patch.items() if v})
        presence[char_id] = prev
    new_snap["presence"] = presence
    msg["presence_snapshot"] = new_snap
    msg.setdefault("metadata", {}).setdefault("applied_edits", []).extend(applied_log)
    msg["metadata"][meta_key] = {"edits": edits, "raw_response": raw}
    return {"ran": True, "skipped": None, "edits": edits, "applied_log": applied_log, "raw": raw}


def run_extra_pass(
    *, conversation, msg, speaker_id, pass_id,
    model=None, options=None,
):
    """Run one extra auto-state pass (transparency/location) against msg
    and apply its edits in place. Returns a summary dict."""
    spec = _EXTRA_PASSES.get(pass_id)
    if not spec:
        return {"ran": False, "skipped": "unknown_pass", "edits": [], "applied_log": [], "raw": ""}
    build_fn, filter_fn, meta_key = spec
    prompt = build_fn(conversation=conversation, speaker_id=speaker_id,
                      message_body=msg.get("content") or "", leaf_id=msg["id"])
    try:
        raw = chat_sync(system=prompt["system"], messages=prompt["messages"],
                        model=model, options=options or {}, think=False)
    except Exception:
        return {"ran": False, "skipped": "model_error", "edits": [], "applied_log": [], "raw": ""}
    _, edits = extract_edits(raw)
    edits = filter_fn(edits, speaker_id)
    if not edits:
        return {"ran": True, "skipped": None, "edits": [], "applied_log": [], "raw": raw}
    return _apply_pass_edits(conversation, msg, edits, raw, meta_key)
