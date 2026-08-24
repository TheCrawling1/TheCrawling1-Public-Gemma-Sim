"""Per-persona prompt assembly.

Goals (mirroring SillyTavern's approach where it makes sense):
- Compact, plain-text descriptions instead of JSON dumps so prompt-eval stays fast.
- Stable system prefix so Ollama can reuse its KV cache between turns.
- Dialogue examples are emitted as primer chat turns (separated by ***), not
  baked into the system prompt — better stylistic priming.
- Author's note injected as a system message at a configurable depth from
  the bottom of the chat history.
- Post-history instructions appended as the very last system message right
  before the model's turn.
- Stop strings auto-derived from character + user names so the model can't
  bleed past its own turn.
- Macros ({{user}}, {{char}}, {{random:a,b}}, {{roll:dN}}) substituted in
  every user-authored field.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from . import entities as ent_mod
from .conversations import active_path, path_to_root
from .entities import load_instance_entities
from .macros import apply as apply_macros


PERSONA_USER = "user"
PERSONA_NARRATOR = "narrator"
EXAMPLE_SEPARATOR = "***"


# Default system prompt templates. These are user-overridable via
# settings.system_prompt_character / settings.system_prompt_narrator
# and support {{user}}, {{char}}, {{random:…}}, {{roll:…}} macros.

DEFAULT_SYSTEM_CHARACTER = (
    "You are {{char}}, a real person in a physical scene. The user is "
    "roleplaying as {{user}}.\n"
    "\n"
    "Write in immersive, present-tense prose. Anchor every reply in "
    "physical detail — what {{char}} is doing, where her weight is, "
    "how her clothes sit, the small physical tells of mood (a held "
    "breath, a tilt of the head, the way her hair falls, the warmth "
    "of skin, the catch of fabric on a curve). But dialogue is how "
    "characters engage with each other: when someone speaks to "
    "{{char}}, the reply opens by responding to what they said, and "
    "the body description wraps around that response — it doesn't "
    "replace it. Look at whoever you're talking to.\n"
    "\n"
    "If {{user}} (or another character) asks {{char}} a direct "
    "question, answer the question in dialogue first; the body "
    "description follows the answer, not the other way round. If "
    "{{user}} greets {{char}} by name or addresses her directly, "
    "acknowledge them in your reply. A reply that's only narration "
    "with no speech is fine when no one addressed {{char}} this turn; "
    "when someone did, respond.\n"
    "\n"
    "Anchor every reply in present tense and concrete sensory detail "
    "— sight, touch, sound, smell, temperature. Use specific verbs "
    "and nouns over generic ones. Show, don't summarize: instead of "
    "\"she's nervous,\" let her wring the hem of her shirt or stare "
    "at the floor.\n"
    "\n"
    "Stay in character. Speak only as yourself unless asked otherwise. "
    "You only know what you have personally seen, heard, or experienced. "
    "Don't narrate {{user}}'s actions or speak their dialogue.\n"
    "\n"
    "When reacting to what {{user}} is wearing or visibly showing, name "
    "the specific items in dialogue. Don't use vague substitutes like "
    "\"that,\" \"everything,\" or \"out.\" Be direct about what's in "
    "view, in your own voice.\n"
    "\n"
    "Format: *asterisks* for actions and physical description, "
    "\"quotes\" for spoken dialogue. Aim for two to four sentences of "
    "physical / environmental description per reply alongside the "
    "dialogue, leaning longer in emotionally charged or quiet, "
    "intimate moments where the prose should breathe.\n"
    "\n"
    "If an \"Example dialogue\" section appears earlier in the prompt, "
    "treat it as reference only — a sample of {{char}}'s voice patterns "
    "and reactions. Do not copy any sentence from it verbatim into your "
    "reply. Do not reuse the opening of an example response as your "
    "opening. Synthesize a fresh reaction grounded in the current scene."
)

DEFAULT_SYSTEM_NARRATOR = (
    "You are the Narrator of an interactive fiction scene. Your job is "
    "to render the world in physical, descriptive prose — what bodies "
    "are doing, what the room looks and feels like, what changes from "
    "moment to moment.\n"
    "\n"
    "Lead with the physical. Describe characters' postures, breathing, "
    "the set of their shoulders, the weight of their clothing, the "
    "exact way someone moves a hand. Pull specifics from the room: "
    "the angle of light, the temperature of the air, the texture of "
    "fabric on furniture, faint sounds beyond the wall. Use precise "
    "verbs and nouns. Show changes through physical detail rather than "
    "narration of feelings — a flush rising on a collarbone, a held "
    "breath finally let out, the way a strap shifts when she moves.\n"
    "\n"
    "Keep present tense. Aim for two or three short paragraphs that "
    "advance the scene with sensory grounding. Use *asterisks* for "
    "action / description and \"quotes\" for any dialogue you voice. "
    "Don't speak as the user.\n"
    "\n"
    "When state changes occur, mark them with directive lines (one per "
    "line, on their own line). The host parses and applies them, then "
    "strips them from the rendered message.\n"
    "\n"
    "  [move <character_id> -> <room_id>]                   # change room\n"
    "  [move <character_id> -> <location_id>:<room_id>]     # change location+room\n"
    "  [outfit <character_id> -> <outfit_id>]               # swap to whole outfit preset (clears + populates worn)\n"
    "  [equip <character_id>.<slot> = <piece_id>]           # swap ONE slot (other slots untouched)\n"
    "  [equip <character_id>.<slot> = <piece_id> state=<name>]  # equip with explicit state\n"
    "  [unequip <character_id>.<slot>]                      # remove the piece in one slot\n"
    "  [set <entity_id>.<dotted.path> = <value>]            # patch a field\n"
    "  [unset <entity_id>.<dotted.path>]                    # clear a field\n"
    "  [next: <character_id>]                               # hand the next turn to a specific character\n"
    "\n"
    "All ids are case-sensitive and lowercase — use `iris`, not "
    "`Iris`. Only use ids that appear in the [Cast] / [World] sections "
    "of this prompt; do not invent new ones.\n"
    "\n"
    "For a whole-outfit swap (change into bikini, change back into "
    "uniform, etc.) use `[outfit <character_id> -> <outfit_id>]` with "
    "an outfit id listed under that character in [Cast]. `[outfit ...]` "
    "sets the baseline — every slot is reset to that outfit's preset, "
    "and any slot not in the preset is cleared.\n"
    "\n"
    "For mix-and-match — keeping most of an outfit but swapping ONE "
    "piece — use `[equip <character_id>.<slot> = <piece_id>]`. This "
    "replaces just that slot's piece; every other equipped piece "
    "stays in place. Example: a character in their school uniform "
    "having a scarf added — emit `[equip alex."
    "neck = wool_scarf]`; the uniform shirt, trousers, "
    "underwear, and shoes all stay, the scarf sits at "
    "the neck. To remove a piece without swapping in a "
    "replacement, use `[unequip <character_id>.<slot>]`. Slot names: "
    "top, bottom, bra, underwear, phallus, pantyhose, gloves, legwear, "
    "shoes, head, face, neck, back, overlay. `underwear` is the "
    "underwear layer (gender-neutral). `phallus` is an "
    "optional under-layer slot beneath underwear — only "
    "meaningful on characters that define it.\n"
    "\n"
    "For state tweaks on an already-equipped piece (shirt rolled up "
    "while staying on, bra unhooked, briefs pulled aside, trousers "
    "unzipped) use `[set <character_id>.properties.worn.<slot>.state "
    "= <state_name>]`. The state name must be one the piece declares — "
    "check the [Available clothing] block for each piece's supported "
    "states. Don't invent state names; if a piece doesn't list a "
    "matching state, the override silently no-ops.\n"
    "\n"
    "Never write a fenced ```edits``` patch with an ad-hoc field like "
    "`clothing.shirt_status` or `wearing_bra` — those are silently "
    "ignored by the renderer.\n"
    "\n"
    "`<value>` in `[set ...]` may be JSON (true/false/numbers/strings/"
    "arrays) or a bare word (treated as a string). Only emit a directive "
    "when the change is real and concrete — moving rooms, changing "
    "clothing, marking a body part state, etc. If you don't need to "
    "change anything, write nothing.\n"
    "\n"
    "`[next: <character_id>]` declares who should speak next when the "
    "scene is in auto turn mode. Use it when you're addressing a "
    "specific character and want them to respond next (e.g. you ask "
    "Dex a direct question — emit `[next: dex]`). Use `[next: user]` "
    "to hand the floor back to the user. Don't emit `[next: ...]` for "
    "your own id (self-handoffs are ignored). When unsure who should "
    "respond next, omit the directive and the host will pick from "
    "name-mentions or round-robin order.\n"
    "\n"
    "For arbitrary edits the directives can't express, append a fenced "
    "edits block at the very end of your message:\n"
    "  ```edits\n"
    "  [\n"
    "    {\"target\": \"<entity_id>\", \"patch\": {<partial fields>}},\n"
    "    {\"target\": \"<entity_id>\", \"replace\": {<full entity>}}\n"
    "  ]\n"
    "  ```\n"
    "Use `patch` for a shallow merge and `replace` for a full overwrite. "
    "Reach for this only when no directive line covers the change — for "
    "outfit swaps and movement, the directive lines above are always "
    "the right tool."
)


# Per-slot wardrobe-override directive — gated on whether any character
# in scope uses the `combined` (sprite-layered) image format. Tagged-
# catalog and unconfigured characters don't read clothing_overrides at
# render time, so teaching the directive for them just invites no-op
# edits cluttering the conversation state. Built dynamically by
# clothing_overrides_instruction() and injected by the prompt-assembly
# layer that owns the surrounding system block.
_CLOTHING_OVERRIDES_BODY = (
    "To toggle a single garment without spinning up a new outfit (no "
    "bra under the shirt, skirt hiked up, shoes kicked off, blouse "
    "ripped open), use:\n"
    "\n"
    "  [set <character_id>.properties.clothing_overrides.<slot> = <state>]\n"
    "\n"
    "Slots (exactly these eight names): top, bottom, bra, underwear, "
    "pantyhose, gloves, legwear, shoes. `underwear` covers panties / "
    "briefs / boxers / jockstraps (gender-neutral). The legacy name "
    "`panties` still works as an alias for `underwear`.\n"
    "\n"
    "State (exactly these three integer values):\n"
    "  1 = worn normally / on / intact\n"
    "  2 = displaced / partial / rolled up / pulled aside / hiked up / "
    "half off / ripped / torn — anything that isn't fully on and isn't "
    "fully off. The exact reading is determined by the outfit and the "
    "scene context (a rolled-up cotton shirt vs. a ripped silk blouse "
    "both land on state 2).\n"
    "  3 = removed / off / discarded / lost\n"
    "\n"
    "Values MUST be the integers 1, 2, or 3. Do not use string values "
    "like \"off\" or \"on\" — those silently fail. Do not invent paths "
    "like `notes.clothing` or `properties.notes.bra` — those land on the "
    "relationship-notes slot and never reach the wardrobe renderer; "
    "only `properties.clothing_overrides.<slot>` with one of the eight "
    "slot names above is read by the renderer. Do not invent fields "
    "like `wearing_bra` or `shirt_status` or `no_bra` either. The "
    "override sits on top of the current outfit's preset slots and "
    "clears automatically on the next `[outfit ... -> ...]` swap.\n"
    "\n"
    "To make a single garment see-through (wet shirt, sheer fabric, "
    "translucent stockings), use:\n"
    "\n"
    "  [set <character_id>.properties.clothing_transparency.<slot> = <0..100>]\n"
    "\n"
    "0 = fully invisible, 100 = fully opaque (default). 50 reads as "
    "half-see-through. Same eight slot names as clothing_overrides; the "
    "value is an integer percent. Combine with clothing_overrides freely "
    "(a slot can be both `2` (rolled up) and `50` (translucent)). Like "
    "clothing_overrides, transparency clears on the next "
    "`[outfit ... -> ...]` swap.\n"
    "\n"
    "To recolor / restyle a templatable outfit (e.g., bikini_generic "
    "carries `{color}` placeholders so the same outfit reads as gold, "
    "black, or pink), use the per-character outfit-overrides overlay:\n"
    "\n"
    "  [set <character_id>.properties.outfit_overrides.color = <value>]\n"
    "  [set <character_id>.properties.outfit_overrides.material = <value>]\n"
    "  [set <character_id>.properties.outfit_overrides.fit = <value>]\n"
    "  [set <character_id>.properties.outfit_overrides.style = <value>]\n"
    "\n"
    "These are per-character; they only affect placeholders in the "
    "current outfit's prose. Quote the value if it has spaces — "
    "`\"gold lamé\"`. Unset slots fall back to the outfit's own default "
    "field, then to empty (the placeholder collapses cleanly).\n"
    "\n"
    "To mix-and-match clothing alongside the primary outfit (cat ears "
    "+ cat tail + cat gloves on top; or a gold bikini hidden under a "
    "school uniform), use:\n"
    "\n"
    "  [set <character_id>.properties.accessories = [\"<id>\", ...]]\n"
    "\n"
    "Each layered piece is itself an outfit-shape entity. Two flavours, "
    "differentiated by the outfit's `properties.under` flag:\n"
    "\n"
    "  - **Over-layer** (default, `under: false` or absent): the "
    "layered piece's coverage replaces the primary's on shared body "
    "parts. Use for visible accessories — cat ears, cat tail, gloves, "
    "tattoo overlays. The accessory's body-part description wins.\n"
    "  - **Under-layer** (`under: true` on the file): the layered "
    "piece is hidden beneath the primary. Contributes slot occupation "
    "and per-slot garment ids (so sprite rendering shows the under-"
    "garment in its slot) but does NOT override the primary's body-"
    "part descriptions. Use for under-layers hidden by outerwear: a "
    "swim top under a school uniform, a sports bra under a t-shirt.\n"
    "\n"
    "A piece may also declare `displaces: [\"<slot>\"]` to clear "
    "named slots on the primary before contributing its own (cat "
    "gloves displaces the primary's gloves cleanly). The accessories "
    "list is wholesale-replaced by deep-merge, not appended — emit a "
    "new full list to add or remove items.\n"
    "\n"
    "For persistent body marks (tattoos, piercings) that stay across "
    "outfit changes and surface even when the part is covered, use:\n"
    "\n"
    "  [set <character_id>.properties.body_marks.<part> = <description>]\n"
    "\n"
    "Where `<part>` is one of the character's body_parts keys (chest, "
    "waist, back, arms, etc.). Distinct from accessories: body marks "
    "are on the character itself and persist across [outfit ...] swaps."
)

# Compact version for CHARACTER personas voicing a beat. The full body above
# teaches six directive families (overrides, transparency, recolor,
# accessories, body marks) — scene-management the NARRATOR needs but a
# character in-scene almost never emits. A character realistically only
# changes how its OWN garment sits (shirt pulled up, bra off), which is the
# `clothing_overrides.<slot> = 1/2/3` directive alone. Keeping just that
# preserves the "different clothing states" capability while dropping ~950
# tokens per character turn — and that saving stacks across every partner
# in a multi-response chain. The narrator still gets the full manual.
_CLOTHING_OVERRIDES_BODY_CONCISE = (
    "To change how ONE of your own garments sits — pulled up, rolled down, "
    "unbuttoned, hiked up, kicked off, ripped open — without swapping your "
    "whole outfit, emit this directive inline in your reply:\n"
    "\n"
    "  [set {self_id}.properties.clothing_overrides.<slot> = <state>]\n"
    "\n"
    "Slots (exactly these eight): top, bottom, bra, underwear, pantyhose, "
    "gloves, legwear, shoes. State is a single integer:\n"
    "  1 = worn normally   2 = displaced / partial (pulled up, rolled, "
    "hiked, half off, ripped, pulled aside)   3 = removed / off\n"
    "\n"
    "Use only the integers 1/2/3 (never the words \"on\"/\"off\"), and only "
    "this exact path — invented field names silently fail. The override "
    "clears on the next outfit swap. Emit it ONLY when a garment actually "
    "changes; describe unchanged clothes in prose, not with a directive."
)


def clothing_overrides_instruction(
    entities: dict[str, dict[str, Any]] | None,
    *,
    exclude_worn: bool = False,
    concise: bool = False,
    self_id: str | None = None,
) -> str:
    """Return the wardrobe-overrides block, scoped to characters in
    `entities` that declare the `combined` image format. Returns "" when
    no such character is present so non-sprite scenes don't get told
    about a directive that would silently no-op for them.

    `exclude_worn` drops characters that carry a v2 ``properties.worn``
    slot map — they're handled by the v2 grammar, and teaching them the
    legacy ``clothing_overrides`` integers alongside it just primes the
    model to reach for v1. Callers that emit the v2 block in the same
    prompt (the edit/add flows) pass ``exclude_worn=True`` so a scene
    full of dual v2+sprite characters never re-teaches v1. The live-turn
    caller keeps the default (v1 stays available there for now)."""
    if not entities:
        return ""
    from .sprite_url import image_format
    from . import bside

    combined = [
        e for e in entities.values()
        if e.get("type") == "character"
        and image_format(bside.image_view(e, entities)) == "combined"
        and not (exclude_worn and (e.get("properties") or {}).get("worn"))
    ]
    if not combined:
        return ""
    # Character personas get the compact single-directive version (they only
    # ever tweak how their own garment sits); the narrator keeps the full
    # scene-management manual (transparency, recolor, accessories, marks).
    # Anchor the character's real id in the example — the character prompt
    # otherwise carries only the display name, so `<your_id>` would leave the
    # model guessing "Iris" vs the `iris` id the renderer needs.
    if concise:
        return _CLOTHING_OVERRIDES_BODY_CONCISE.replace(
            "{self_id}", self_id or "<your_id>"
        )
    names = ", ".join(
        f"`{e.get('id')}`" for e in sorted(combined, key=lambda x: x.get("id") or "")
    )
    return (
        f"For sprite-rendered characters ({names}) the [outfit] "
        f"directive only swaps the whole preset; per-slot tweaks live "
        f"on the character.\n\n{_CLOTHING_OVERRIDES_BODY}"
    )


# ---------------------------------------------------------------------------
# v2 slot-based wardrobe directives (the modern path). Injected into the
# narrator-edit / narrator-add prompts for any character that carries a
# `properties.worn` slot map, so those flows stop reaching for the v1
# `clothing_overrides` integers on v2 characters. The live narrator turn
# already teaches this grammar (DEFAULT_SYSTEM_NARRATOR); this is the
# same vocabulary, plus a per-character [Available clothing] listing so
# the model uses real piece ids and declared state names.
# ---------------------------------------------------------------------------
_WARDROBE_V2_BODY = (
    "For the slot-based characters below ({names}), change clothing with "
    "these directives:\n"
    "\n"
    "  [outfit <char> -> <outfit_id>]                            swap the WHOLE outfit (resets every slot to that preset)\n"
    "  [equip <char>.<slot> = <piece_id>]                        put ONE piece in a slot, leaving the other slots as they are\n"
    "  [equip <char>.<slot> = <piece_id> state=<state_name>]     same, in a specific declared state\n"
    "  [unequip <char>.<slot>]                                   take the piece off that slot (the part goes bare)\n"
    "  [set <char>.properties.worn.<slot>.state = <state_name>]  tweak an already-worn piece (unbuttoned / ripped / pulled aside / off)\n"
    "\n"
    "Use only the piece ids and state names shown under [Available "
    "clothing] below. A state a piece does not declare silently no-ops — "
    "do not invent state names. Slot names: top, bottom, bra, underwear, "
    "phallus, pantyhose, gloves, legwear, shoes, head, face, neck, back, "
    "overlay.\n"
    "\n"
    "Common partial changes: to rip a piece open, set it to its "
    "ripped/torn state — e.g. `[set <char>.properties.worn.top.state = "
    "ripped]`; to take a piece off entirely, use `[unequip <char>."
    "<slot>]` or set its state to off. Always pick a state the piece "
    "actually lists below.\n"
    "\n"
    "Only emit a clothing directive when the piece or its state actually "
    "CHANGES. A piece already shown under 'worn now' in that same state "
    "needs no directive — re-equipping what a character is already "
    "wearing is a wasted no-op. Describe unchanged clothes in prose, not "
    "with directives.\n"
    "\n"
    "[Available clothing]\n"
    "{available}"
)

# Worked example appended after the v2 body — a rip, the exact
# partial-clothing case where the model otherwise reflexively reaches
# for the legacy `clothing_overrides` integers.
_WARDROBE_V2_EXAMPLE = (
    "\n\n[Worked example — change one garment (slot-based wardrobe)]\n"
    "\n"
    "Target message (speaker=Narrator): Mira stands at the window in her "
    "tactical bodysuit, sealed to the collar.\n"
    "Directive: rip the front of her bodysuit open across the chest\n"
    "\n"
    "Expected output:\n"
    "[set mira.properties.worn.top.state = ripped]\n"
    "\n"
    "Mira stands at the window, the front of her bodysuit torn open across "
    "the chest, the fabric gaping wide. The rest of the suit is exactly as "
    "it was."
)

# The legacy v1 worked example — kept ONLY for sprite characters that
# still read clothing_overrides. Was previously hardcoded in each
# narrator flow's _WARDROBE_EXTRA_TEMPLATE.
_WARDROBE_V1_EXAMPLE = (
    "\n\n[Worked example — wardrobe override]\n"
    "\n"
    "Target message (speaker=Narrator): Nadia stands at the counter, the "
    "white shirt rolled up under her collarbones, her bra "
    "plainly visible beneath the open cardigan.\n"
    "Directive: no bra\n"
    "\n"
    "Expected output:\n"
    "[set nadia.properties.clothing_overrides.bra = 3]\n"
    "\n"
    "Nadia stands at the counter, the white shirt sleeves rolled up, "
    "her collarbones visible above the open "
    "cardigan. The dark trousers and canvas shoes are "
    "exactly as they should be."
)


def _wardrobe_v2_available_block(
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]],
) -> str:
    """Per-character [Available clothing] listing: the pieces currently
    in `worn` (with current + declared states) and the other pieces the
    character owns (via their outfits' equips + any loose clothing
    entities) that `[equip]` can reach. Piece states are resolved
    through the global catalog when not instanced."""
    from . import clothing_v2

    cid = char.get("id") or ""
    props = char.get("properties") or {}
    worn = props.get("worn") or {}
    cur_outfit = props.get("current_outfit")

    header = f"  - {cid}"
    if cur_outfit:
        header += f" (wearing outfit: {cur_outfit})"
    header += ":"
    lines = [header]

    def _states(pid: str) -> list[str]:
        piece = clothing_v2._resolve_piece(pid, entities)
        st = (piece.get("properties") or {}).get("states") if piece else None
        return [s for s in (st or []) if isinstance(s, str)]

    worn_piece_ids: set[str] = set()
    worn_lines: list[str] = []
    for slot, entry in worn.items():
        if not isinstance(entry, dict):
            continue
        pid = entry.get("piece")
        if not pid:
            continue
        worn_piece_ids.add(pid)
        states = _states(pid)
        cur = entry.get("state") or (states[0] if states else "on")
        slist = ", ".join(states) if states else "on, off"
        worn_lines.append(f"        {slot} = {pid}  (state {cur}; states: {slist})")
    if worn_lines:
        lines.append("      worn now:")
        lines.extend(worn_lines)

    # Other pieces the character owns: gather from every registered
    # outfit's equips, plus any loose clothing entity owned by them.
    avail: dict[str, str] = {}
    outfit_ids = list(props.get("outfits") or [])
    if cur_outfit and cur_outfit not in outfit_ids:
        outfit_ids.append(cur_outfit)
    for oid in outfit_ids:
        bundle = clothing_v2._resolve_outfit_bundle(oid, entities)
        equips = (bundle.get("properties") or {}).get("equips") if bundle else None
        if isinstance(equips, dict):
            for slot, pid in equips.items():
                if isinstance(pid, str):
                    avail.setdefault(pid, slot)
    for e in entities.values():
        if not isinstance(e, dict) or e.get("type") != "clothing":
            continue
        if (e.get("properties") or {}).get("owner") != cid:
            continue
        pid = e.get("id")
        if isinstance(pid, str):
            avail.setdefault(pid, (e.get("properties") or {}).get("slot") or "?")

    other_lines: list[str] = []
    for pid, slot in sorted(avail.items()):
        if pid in worn_piece_ids:
            continue
        states = _states(pid)
        slist = ", ".join(states) if states else "on, off"
        other_lines.append(f"        {pid} ({slot}; states: {slist})")
    if other_lines:
        lines.append("      other pieces available to [equip]:")
        lines.extend(other_lines)

    return "\n".join(lines)


def wardrobe_v2_instruction(
    entities: dict[str, dict[str, Any]] | None,
) -> str:
    """Return the v2 slot-based wardrobe block, scoped to characters in
    `entities` that carry a non-empty `properties.worn` slot map. Returns
    "" when no such character is present (so v1-only / sprite scenes
    don't get told about a grammar they can't use)."""
    if not entities:
        return ""
    v2_chars = [
        e for e in entities.values()
        if isinstance(e, dict) and e.get("type") == "character"
        and isinstance((e.get("properties") or {}).get("worn"), dict)
        and (e.get("properties") or {}).get("worn")
    ]
    if not v2_chars:
        return ""
    v2_chars.sort(key=lambda x: x.get("id") or "")
    names = ", ".join(f"`{e.get('id')}`" for e in v2_chars)
    available = "\n".join(
        _wardrobe_v2_available_block(c, entities) for c in v2_chars
    )
    return _WARDROBE_V2_BODY.format(names=names, available=available)


def compose_wardrobe_extra(
    entities: dict[str, dict[str, Any]] | None,
) -> str:
    """The wardrobe instruction block appended to the narrator-edit /
    narrator-add prompts. Emits the v2 slot-based grammar for any
    character with a `worn` map, and the legacy v1 clothing_overrides
    grammar for any sprite-rendered character — each block names the
    characters it applies to, so a mixed scene gets both without
    contradiction. Returns "" (no leading newline) when neither applies.
    """
    parts: list[str] = []
    v2 = wardrobe_v2_instruction(entities)
    if v2:
        parts.append(v2 + _WARDROBE_V2_EXAMPLE)
    # Only teach v1 for characters that are NOT v2 (no worn map). A scene
    # of dual v2+sprite characters (e.g. the bookshop cast) would otherwise
    # get the clothing_overrides example alongside the v2 block, priming
    # the model to emit v1 even on pure-v2 characters like Iris.
    v1 = clothing_overrides_instruction(entities, exclude_worn=True)
    if v1:
        parts.append(v1 + _WARDROBE_V1_EXAMPLE)
    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def assemble_prompt(
    conversation: dict[str, Any],
    persona: str,
    *,
    speaker_id: str | None = None,
    leaf_id: str | None = None,
    cfg_default_options: dict[str, Any] | None = None,
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the full prompt for `persona`.

    `extra_context` is a dict merged into the per-call settings that the
    prompt registry's blocks can read off `ctx.settings`. Used to feed
    call-specific data into registry blocks without widening the
    PromptContext schema — e.g. the stream route passes
    `{"_multi": {...}}` so the multi-character voice + directive blocks
    fire (see app/multi_response.py).

    `leaf_id` overrides the conversation's stored `active_path_leaf` for
    history + effective-state resolution. Regen passes the parent of the
    message-being-replaced so the original reply (still pointed at by
    the on-disk `active_path_leaf` until the new generation persists)
    doesn't leak into the prompt.

    `cfg_default_options` is the global ollama.default_options dict, used by
    the truncator to read num_ctx / num_predict if the conversation hasn't
    overridden them. Optional; defaults pulled from app.config when omitted.
    """
    if persona == PERSONA_USER:
        return {
            "system": "",
            "messages": [],
            "pieces": [{"label": "User persona", "content": "User composes their own messages."}],
            "stop": [],
        }

    if cfg_default_options is None:
        try:
            from flask import current_app
            cfg_default_options = (current_app.config.get("ollama") or {}).get("default_options") or {}
        except Exception:
            cfg_default_options = {}

    # Path-based effective state: instance entity files are baselines,
    # `metadata.applied_edits` along the active path are replayed on top.
    # This keeps branches isolated — switching to a sibling root shows
    # only that branch's accumulated edits, never the other root's.
    from .effective import (
        effective_entities_at,
        effective_scenario_instructions,
        effective_user_persona,
    )
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    history = path_to_root(conversation, leaf_id) if leaf_id else []
    instance_entities = effective_entities_at(conversation, leaf_id)
    # Branch isolation: a character added on a sibling branch via
    # cast_add lives as an instance file on disk shared by every
    # branch — effective_entities_at returns them regardless. Filter
    # by this branch's effective cast so only characters / objects
    # actually on the active path appear in the focal's prompt. The
    # speaker themselves is kept regardless (a character generating
    # their own turn always counts as in-cast even if the cast_add
    # is somehow missing).
    from .effective import branch_filter
    instance_entities = branch_filter(conversation, leaf_id, instance_entities)
    # If we filtered out the focal speaker (e.g. a generate request
    # for someone not on this branch's cast), put them back so the
    # _assemble_character lookup works. The caller chose them
    # deliberately; this is the wrong layer to reject.
    if speaker_id and speaker_id not in instance_entities:
        full = effective_entities_at(conversation, leaf_id)
        if speaker_id in full:
            instance_entities[speaker_id] = full[speaker_id]
    # Scene staging scoping is handled by branch_filter above: the
    # entity map is already narrowed to this branch's effective cast
    # (which applies the scene_staging_picks whitelist and replays
    # cast_add/cast_remove), so the [Cast] block scopes correctly
    # without a separate pick list — and re-added characters surface.
    settings = dict(conversation.get("settings", {}) or {})
    settings["user_persona"] = effective_user_persona(conversation, leaf_id)
    settings["scenario_instructions"] = effective_scenario_instructions(conversation, leaf_id)
    if extra_context:
        settings.update(extra_context)

    if persona == PERSONA_NARRATOR:
        return _assemble_narrator(conversation, instance_entities, history, settings, cfg_default_options)
    return _assemble_character(
        conversation,
        instance_entities,
        history,
        settings,
        character_id=speaker_id or persona,
        cfg_default_options=cfg_default_options,
    )


# ---------------------------------------------------------------------------
# Narrator
# ---------------------------------------------------------------------------


def _assemble_narrator(
    conversation: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
    settings: dict[str, Any],
    cfg_default_options: dict[str, Any],
) -> dict[str, Any]:
    # System prompt blocks come from the registry — one source of truth
    # for both character and narrator paths. See `app/prompt/core.py`
    # for the registered block list. Message-pipeline blocks (summary,
    # author note, post-history) and truncation stay in this function;
    # they shape the messages array, not the system_text.
    from .prompt import PromptContext, assemble
    user_persona = settings.get("user_persona") or {}
    user_name = (user_persona.get("name") or "").strip() or "User"
    ctx = {"user_name": user_name, "char_name": "Narrator", "user_persona": user_persona}

    pctx = PromptContext(
        conversation=conversation,
        persona="narrator",
        focal_id=None,
        focal=None,
        entities=entities,
        history=history,
        settings=settings,
        macros=ctx,
        presence={},
        leaf_id=conversation.get("active_path_leaf") or "",
    )
    assembled = assemble(pctx)
    system_text = assembled.system
    pieces: list[dict[str, str]] = list(assembled.pieces)

    lore = _activated_lore(entities, history)
    summary, stale_summary = _resolve_summary_text(settings, history, ctx)
    if stale_summary:
        pieces.append({
            "label": "Summary so far (stale fragments, hidden)",
            "content": stale_summary,
        })
    if summary:
        pieces.append({"label": "Summary so far", "content": summary})

    # Skip empty-bodied messages (setup roots, and the structured
    # `narrator_state` move/outfit beats which carry no prose) — same rule the
    # character path uses, so they don't render as a bare "Narrator:" turn.
    history_msgs = [
        _history_msg(m, entities, ctx, focal_id="narrator")
        for m in history
        if (m.get("content") or "").strip()
    ]

    messages: list[dict[str, str]] = []
    if summary:
        messages.append({"role": "system", "content": f"[Summary so far]\n{summary}"})
    messages.extend(history_msgs)
    messages = _inject_lore_at_depth(messages, lore.get("at_depth") or [], ctx)
    messages = _inject_author_note(messages, settings, ctx, pieces)
    messages = _append_post_history(messages, settings, ctx, pieces)

    messages, dropped, dropped_msgs = _truncate_history(messages, system_text, settings, cfg_default_options)
    pieces.append({"label": "Conversation history (sent)", "content": _history_preview(messages)})
    if dropped:
        pieces.append({"label": "Truncation", "content": f"{dropped} message(s) elided to fit num_ctx."})

    return {
        "system": system_text,
        "messages": messages,
        "pieces": pieces,
        "stop": _stop_strings(entities, user_name),
        "dropped_messages": dropped_msgs,
    }


# ---------------------------------------------------------------------------
# Character
# ---------------------------------------------------------------------------


def _assemble_character(
    conversation: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
    settings: dict[str, Any],
    character_id: str,
    cfg_default_options: dict[str, Any],
) -> dict[str, Any]:
    char = entities.get(character_id)
    if not char or char.get("type") != "character":
        raise ValueError(f"Character {character_id!r} not in instance.")

    name = char.get("name") or character_id
    user_persona = settings.get("user_persona") or {}
    user_entity = entities.get("user") or {}
    user_name = (
        (user_entity.get("name") or "").strip()
        or (user_persona.get("name") or "").strip()
        or "User"
    )
    ctx = {"user_name": user_name, "char_name": name, "user_persona": user_persona}

    # Perceive-only user identity: a focal who doesn't KNOW the user (a stranger)
    # renders {{user}} — and the user-persona block's header — as a descriptor,
    # not the name. The real `user_name` above is kept for stop strings (they
    # detect the model speaking AS the user, keyed on the real turn label); only
    # the macro view is masked. The user-persona block additionally strips the
    # description/identity for an unknown user (appearance is all a stranger sees).
    if not perceives_user_identity(
        character_id, conversation, (user_persona.get("role") or "").strip().lower()
    ):
        ctx["user_name"] = user_descriptor(user_persona)

    # Pull the focal character's outfit from their latest presence-snapshot
    # so scenario starting_state and narrator [outfit ...] directives flow
    # through. Used here for the message pipeline; the registry's
    # focal_character block re-derives this internally.
    presence = _latest_presence_for(history, character_id)

    # System prompt blocks come from the registry — one source of truth
    # for both character and narrator paths. See `app/prompt/core.py`
    # for the registered block list. Message-pipeline blocks
    # (example_dialogue, summary, author note, post-history,
    # same-speaker-continue, right-now) and truncation stay in this
    # function; they shape the messages array, not the system_text.
    from .prompt import PromptContext, assemble
    pctx = PromptContext(
        conversation=conversation,
        persona="character",
        focal_id=character_id,
        focal=char,
        entities=entities,
        history=history,
        settings=settings,
        macros=ctx,
        presence=presence or {},
        leaf_id=conversation.get("active_path_leaf") or "",
    )
    assembled = assemble(pctx)
    system_text = assembled.system
    pieces: list[dict[str, str]] = list(assembled.pieces)

    lore = _activated_lore(entities, history)
    summary, stale_summary = _resolve_summary_text(settings, history, ctx, focal_id=character_id)
    if stale_summary:
        pieces.append({
            "label": "Summary so far (stale fragments, hidden)",
            "content": stale_summary,
        })
    if summary:
        pieces.append({"label": "Summary so far", "content": summary})

    # Player/GM-facing narration is never in a character's history. A Return by
    # Death loop-root beat ("the world snaps back … no one else remembers") is
    # for the human's transcript and the narrator, NOT the cast: an NPC must not
    # be handed the fact that time reset. Applied unconditionally, before the
    # locational filter, so it holds even with locational_memory off.
    char_history = [m for m in history
                    if not (m.get("metadata") or {}).get("hidden_from_characters")]
    locational_memory = settings.get("locational_memory", True)
    filtered = _history_visible_to(char_history, character_id) if locational_memory else char_history
    # Pass focal_id so _history_msg can correctly role-map: only the
    # focal character's own past turns get role="assistant"; everything
    # else (other characters' turns including user-typed-as-NPC,
    # narrator beats, user-typed messages) is role="user" — input the
    # focal reacts to. Without this, chat-instruct alternation broke
    # whenever the prompt ended on a non-focal assistant turn: model
    # EOSed immediately, empty completion fell through stream.py's
    # non-empty guard, client removed the placeholder. User-visible
    # symptom was "no response at all" when speaking-as-NPC or
    # generating after another NPC's AI turn.
    # Multi turns: render prior multi-group messages with the SAME
    # numbered-label format the joint directive demands, numbered by
    # the CURRENT roster (lead = 1, partners in order) so old turns
    # never contradict today's numbering. Without this, every persisted
    # multi turn re-renders as a plain "Name: body" counter-example and
    # the model's label discipline erodes with conversation depth
    # (replay run 1: 3/3 → 3/3 → 2/3 → 1/3 over four turns).
    multi_extra = settings.get("_multi") or {}
    multi_roster_numbers: dict[str, int] | None = None
    if multi_extra:
        roster_names = [
            multi_extra.get("lead_name"),
            *(multi_extra.get("partner_names") or []),
        ]
        multi_roster_numbers = {
            str(n).lower(): i
            for i, n in enumerate(roster_names, start=1)
            if isinstance(n, str) and n
        }
    history_msgs = [
        _history_msg(
            m, entities, focal_id=character_id,
            number_multi_groups=multi_roster_numbers,
        )
        for m in filtered
        # Skip empty-bodied messages (e.g. setup roots that exist only
        # to carry a presence snapshot). They'd otherwise render as a
        # bare speaker label — a plain "Narrator:" user turn right
        # before the live turn reads as a voice cue and pulls the model
        # into unmarked third-person narration.
        if (m.get("content") or "").strip()
    ]

    pair_ctx = _pair_context(
        char, entities, presence, settings, conversation=conversation,
    )
    example_msgs = _example_messages(char, ctx, pair_context=pair_ctx)
    if example_msgs:
        pieces.append(
            {
                "label": "Example dialogue (primer)",
                "content": "\n".join(m["content"] for m in example_msgs),
            }
        )
    # Dev-panel readout: which context tags are live and which dialogue-pair
    # sets fired this turn. Informational only (a piece, never system_text).
    _pair_report = _pair_selection_report(char, pair_ctx)
    if _pair_report:
        pieces.append({"label": "Dialogue pair selection", "content": _pair_report})

    messages: list[dict[str, str]] = []
    messages.extend(example_msgs)
    if summary:
        messages.append({"role": "system", "content": f"[Summary so far]\n{summary}"})
    messages.extend(history_msgs)
    messages = _inject_lore_at_depth(messages, lore.get("at_depth") or [], ctx)
    messages = _inject_author_note(messages, settings, ctx, pieces, focal_id=character_id)
    messages = _append_post_history(messages, settings, ctx, pieces)
    # If the most recent message in the focal's visible history is
    # already by the focal character, the model otherwise sees its own
    # complete turn sitting at the bottom of the prompt and tends to
    # emit nothing — there's nothing left to say from its point of
    # view. Append an explicit "continue the scene" instruction so the
    # model produces a fresh beat instead of an empty turn. Triggered
    # by Auto Play (which fires Generate repeatedly on the same lead)
    # and by manually clicking Generate ↻ twice in a row.
    messages = _append_same_speaker_continue(messages, filtered, character_id, name, pieces)

    # [Right now] — wardrobe-deviation anchor at the tail of the
    # messages array. Position fix (system → messages tail) drove
    # engagement from 0/10 to 10/10 on the rolled-up-shirt
    # regression test. Reshaped as a user-role narrator beat so the
    # subject is unambiguous (the focal, not the user).
    right_now = _right_now_block(char, entities, ctx)
    if right_now:
        narrator_beat = f"Narrator: *Right now: {right_now}*"
        messages.append({"role": "user", "content": narrator_beat})
        pieces.append({"label": "Right now", "content": right_now})

    messages, dropped, dropped_msgs = _truncate_history(messages, system_text, settings, cfg_default_options)
    pieces.append({"label": "Conversation history (sent)", "content": _history_preview(messages)})
    if dropped:
        pieces.append({"label": "Truncation", "content": f"{dropped} message(s) elided to fit num_ctx."})

    return {
        "system": system_text,
        "messages": messages,
        "pieces": pieces,
        "stop": _stop_strings(entities, user_name, exclude_id=character_id),
        # Multi-response uses this reduced set (user handback only) so the
        # joint call can voice every roster character back-to-back without
        # a co-present character's `\n<Name>:` stop killing the stream.
        "stop_user_only": _user_stop_strings(user_name),
        "dropped_messages": dropped_msgs,
    }


# ---------------------------------------------------------------------------
# Plain-text descriptions
# ---------------------------------------------------------------------------


def _resolve_state(state_id: str, entities: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Look up a `state` entity by id. Checks the branch's effective
    entities first, then falls back to the global template catalog —
    same idiom `clothing_v2._resolve_piece` uses. States are not
    auto-instanced into every conversation, so the catalog fallback is
    what makes `[state <char> -> drunk]` resolve when `drunk` lives only
    in `data/states/`."""
    st = entities.get(state_id)
    if isinstance(st, dict) and st.get("type") == "state":
        return st
    try:
        from . import entities as _ent_mod
        tmpl = _ent_mod.get(state_id)
        if isinstance(tmpl, dict) and tmpl.get("type") == "state":
            return tmpl
    except Exception:
        pass
    return None


def _apply_active_states(
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Compose a character's `properties.active_states` onto its body.

    Returns ``(char_or_copy, affect_lines, mannerism_lines)``:

      - Each state's ``body_overlays: {part: description}`` overwrites
        the matching ``body_parts.<part>.base`` (later states in the
        list win on a shared part). Done on a deep copy so the on-disk
        entity is untouched.
      - ``affect_summary`` lines are collected for a prominent
        "Current state (active)" block in the card.
      - ``mannerism_overlay`` lines are collected to merge into the
        character's mannerisms block.

    No-op (returns the original char + empty lists) when the character
    has no active states. Overlays only touch parts the character
    already defines — a state never invents a new body part.
    """
    props = char.get("properties") or {}
    active = props.get("active_states")
    if not isinstance(active, list) or not active:
        return char, [], []

    import copy
    new_char = copy.deepcopy(char)
    nprops = new_char.setdefault("properties", {})
    body_parts = nprops.get("body_parts")
    affect: list[str] = []
    manner: list[str] = []
    for sid in active:
        if not isinstance(sid, str) or not sid:
            continue
        st = _resolve_state(sid, entities)
        if not st:
            continue
        sp = st.get("properties") or {}
        summ = sp.get("affect_summary")
        if isinstance(summ, str) and summ.strip():
            affect.append(summ.strip())
        overlays = sp.get("body_overlays")
        if isinstance(overlays, dict) and isinstance(body_parts, dict):
            for part, desc in overlays.items():
                if not isinstance(desc, str) or not desc.strip():
                    continue
                if part in body_parts and isinstance(body_parts[part], dict):
                    # APPEND to the existing base rather than replace it,
                    # so a reusable generic state ("crying", "drunk")
                    # layers its effect on top of the character's own
                    # specific anatomy/hair/eye prose instead of wiping
                    # it. Bespoke states can still fully restate a part
                    # by making the overlay self-contained — it just
                    # reads as base + overlay. Order follows the
                    # active_states list, so later states stack last.
                    existing = (body_parts[part].get("base") or "").strip()
                    overlay = desc.strip()
                    body_parts[part]["base"] = (
                        f"{existing} {overlay}".strip() if existing else overlay
                    )
        mo = sp.get("mannerism_overlay")
        if isinstance(mo, str) and mo.strip():
            manner.append(mo.strip())
    return new_char, affect, manner


def _character_card(
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    ctx: dict[str, Any],
    current_outfit_override: str | None = None,
) -> str:
    """Compact character description: name, description, traits, current outfit.

    Note: dialogue_examples are NOT included here; they're emitted as primer
    chat turns instead (more effective for stylistic priming).
    """
    name = char.get("name") or char.get("id")
    out = [f"Name: {name}"]
    if char.get("description"):
        out.append(apply_macros(char["description"], ctx).strip())

    # Active states (drunk / exhausted / a belief, etc.) overlay the
    # character's body_parts.base and contribute an affect summary +
    # mannerism shift. Applied here, before outfit composition, so BOTH
    # the v1 and v2 body-description paths inherit the overlaid bases for
    # free (they read body_parts.<part>.base). See _apply_active_states.
    char, _state_affect, _state_manner = _apply_active_states(char, entities)
    props = char.get("properties") or {}
    if _state_affect:
        out.append(
            "Current state (active): "
            + " ".join(apply_macros(a, ctx) for a in _state_affect)
        )

    personality = props.get("personality") or {}
    if isinstance(personality, dict) and personality:
        traits = ", ".join(f"{k} {v}" for k, v in personality.items())
        out.append(f"Personality: {traits}.")

    # Mannerisms: concrete physical / behavioural tells the character does
    # automatically. Surfaced here so the model has specific non-anatomical
    # material to reach for when filling action space, instead of defaulting
    # to whatever's most prominent in body_parts.
    mannerisms = props.get("mannerisms")
    if isinstance(mannerisms, list) and mannerisms:
        items = [apply_macros(m, ctx).strip() for m in mannerisms if isinstance(m, str)]
        items = [m for m in items if m]
        if items:
            out.append("Mannerisms (specific things {{char}} does):\n".replace("{{char}}", name) +
                       "\n".join(f"- {m}" for m in items))

    # Clothing-meets-body tells: rules-aware physical tics that hook
    # into the wardrobe state (a t-shirt riding up when she shifts, a
    # bow tilting when she ducks her chin, yoga pants pulling taut
    # across hips when she scoots). Same shape as mannerisms but
    # specifically the garment-on-body fingerprint moments that anchor
    # prose without inventing chest-fabric-template language.
    tells = props.get("signature_physical_tells")
    if isinstance(tells, list) and tells:
        items = [apply_macros(t, ctx).strip() for t in tells if isinstance(t, str)]
        items = [m for m in items if m]
        if items:
            out.append(
                "Physical tells (clothing-meets-body fingerprint moments):\n"
                + "\n".join(f"- {m}" for m in items)
            )

    # Scent — surface the character's signature smell so the model has
    # an olfactory anchor available when proximity moments call for it.
    # Only `scent.general` is rendered here (broad signature); per-part
    # scents (hair, skin, breath, feet) stay reserved for sensitive
    # contexts where the model has already been invited there by the
    # scene rather than auto-surfaced into every reply.
    scent = props.get("scent")
    if isinstance(scent, dict):
        general = scent.get("general")
        if isinstance(general, str) and general.strip():
            out.append(f"Scent: {apply_macros(general.strip(), ctx)}")

    # Non-explicit (SFW) character format. Characters authored in the
    # simplified schema (see docs/dialogue_examples.md / the
    # `Examples need to filled out` references) carry a prose `appearance`
    # map instead of the per-part `body_parts` anatomy table, an
    # `emotional_map`, and a hard `boundaries` content guardrail. Render
    # each cleanly here so they don't fall through to the raw
    # "Current state (narrator-tracked)" dump. `appearance` is rendered
    # regardless of model version — it's an author-supplied prose block,
    # not a derived body description, so it never collides with the
    # body_parts / worn composition above.
    appearance = props.get("appearance")
    if isinstance(appearance, dict) and appearance:
        appear_lines = []
        for k, v in appearance.items():
            if isinstance(v, str) and v.strip():
                label = str(k).replace("_", " ").capitalize()
                appear_lines.append(f"- {label}: {apply_macros(v.strip(), ctx)}")
        if appear_lines:
            out.append("Appearance:\n" + "\n".join(appear_lines))

    emotional_map = props.get("emotional_map")
    if isinstance(emotional_map, dict) and emotional_map:
        emo_lines = []
        for k, v in emotional_map.items():
            if isinstance(v, str) and v.strip():
                label = str(k).replace("_", " ").capitalize()
                emo_lines.append(f"- {label}: {apply_macros(v.strip(), ctx)}")
        if emo_lines:
            out.append("Emotional map:\n" + "\n".join(emo_lines))

    # Boundaries: a per-character content guardrail. Surfaced prominently
    # as its own line (not buried in a state dump) so the model treats it
    # as a hard constraint on how the character may be portrayed.
    boundaries = props.get("boundaries")
    if isinstance(boundaries, str) and boundaries.strip():
        out.append(
            "Content boundary (MUST follow): "
            + apply_macros(boundaries.strip(), ctx)
        )

    outfit = None
    # Presence-snapshot outfit (from scenario starting_state or narrator
    # [outfit ...] directives) wins over the character entity's static
    # current_outfit. Falls back to props.current_outfit if no override.
    outfit_id = current_outfit_override or props.get("current_outfit")

    # v2 clothing dual-read. When the character has a `properties.worn`
    # map (the slot-based source-of-truth from docs/clothing.md), route
    # the outfit-text + body-description composition through
    # clothing_v2 instead of the v1 _resolved_outfit / _body_description
    # path. Falls back to v1 when worn is missing — un-migrated
    # characters keep working unchanged.
    # `worn` key presence (even when empty) signals v2 model — an
    # unclothed character with worn={} still uses v2 composition rules.
    # Empty worn produces body.base everywhere; that's the correct
    # unclothed rendering.
    worn = props.get("worn")
    use_v2 = "worn" in props and isinstance(worn, dict)
    # A v1-style [outfit] swap sets the presence-snapshot outfit override
    # and clears the worn map. When that leaves a v2-capable character
    # with an EMPTY worn map but an explicit presence override pointing to
    # a real v1 outfit (one that carries a coverage map), the swap is the
    # live truth — render it through the v1 path. Without this the v2
    # branch below, which reads `worn` and deliberately ignores the
    # presence override, fires its "no outfit set" anchor for a character
    # who is actually dressed (e.g. swapping a v2 idol into a v1
    # coverage outfit rendered her with no outfit).
    if use_v2 and not worn and current_outfit_override:
        _ov = entities.get(current_outfit_override)
        if not isinstance(_ov, dict):
            _ov = ent_mod.get(current_outfit_override) or {}
        if isinstance(_ov, dict) and ((_ov.get("properties") or {}).get("coverage")):
            use_v2 = False
    if use_v2:
        from . import clothing_v2
        # Backcompat shim: if the narrator emitted v1-style
        # `clothing_overrides` (still taught by the prompt until
        # step 7), translate them into worn state updates before
        # composing. apply_v1_overrides_to_worn returns a copy with
        # worn map updated; original char is untouched. No-op when
        # the character has no clothing_overrides.
        char = clothing_v2.apply_v1_overrides_to_worn(char, entities)
        props = char.get("properties") or {}
        worn = props.get("worn") or {}
        # Under v2, worn IS the live wardrobe state and
        # `properties.current_outfit` is the preset breadcrumb
        # (which preset was last applied). The presence_snapshot's
        # outfit override is v1 semantics — under v2 we ignore it
        # and read the current_outfit field on the entity directly,
        # because the test (and future apply-layer integration)
        # patches the entity's current_outfit when changing to a
        # v2 bundle, not the v1-style presence override.
        v2_outfit_id = props.get("current_outfit") or outfit_id
        outfit = clothing_v2._resolve_outfit_bundle(v2_outfit_id, entities) if v2_outfit_id else None
        outfit = outfit or {}
        sig = clothing_v2.signature_for_outfit(char, outfit, entities)
        if sig:
            # Full preset match → emit the outfit-level signature.
            sig = _apply_outfit_template(sig, outfit, char)
            out.append(f"Currently wearing: {apply_macros(sig, ctx)}")
        elif not worn:
            # Empty-wardrobe case (worn={}). The model's training
            # priors are strong — even Iris's body.base may have
            # residual clothing mentions, and the conversation context
            # primes for "she's in her apron". A weak anchor gets
            # steamrolled, so state the empty-wardrobe fact plainly.
            out.append(
                "Currently wearing: nothing is currently equipped "
                "(no outfit set)."
            )
        else:
            # Partial-deviation case (some pieces equipped, doesn't
            # match preset exactly). Synthesize a tight summary that
            # ALSO calls out what's MISSING from the preset, so the
            # model doesn't fill in the gaps from its training prior
            # (the "uniform" attractor making it describe a skirt
            # that isn't there).
            #
            # Treat state="off" entries as unequipped — they sit in
            # the worn map (so the slot is "tracked") but the piece
            # is conceptually not being worn. Without this, off-state
            # pieces leaked into the "Currently wearing:" list and
            # ALSO weren't flagged as missing from the preset.
            piece_names = []
            for slot in clothing_v2.LAYER_ORDER:
                entry = worn.get(slot) or {}
                pid = entry.get("piece")
                state = (entry.get("state") or "").lower()
                if not pid or state == "off":
                    continue
                piece = clothing_v2._resolve_piece(pid, entities)
                if not piece:
                    continue
                # Annotate non-default states explicitly in the wearing
                # line — "Plain white t-shirt (rolled up)" instead of
                # bare "Plain white t-shirt" — so the model has a
                # second anchor for the deviation beyond the per-part
                # appearance block. Default state is implicit.
                states = (piece.get("properties") or {}).get("states") or []
                default_state = states[0] if states else "on"
                label = piece.get("name") or pid
                if state and state != default_state.lower():
                    label = f"{label} ({state.replace('_', ' ')})"
                piece_names.append(label)
            missing_lines: list[str] = []
            outfit_equips = (outfit.get("properties") or {}).get("equips") or {}
            for slot, expected_piece_id in outfit_equips.items():
                entry = worn.get(slot) or {}
                # Treat slot-missing-from-worn AND state=off as
                # both "preset item currently off".
                if entry and (entry.get("state") or "").lower() != "off":
                    continue
                missing_piece = clothing_v2._resolve_piece(expected_piece_id, entities)
                missing_lines.append(
                    f"{missing_piece.get('name') if missing_piece else expected_piece_id} ({slot})"
                )
            line = "Currently wearing: " + ", ".join(piece_names) + "."
            if missing_lines:
                line += (
                    " NOT wearing (preset items currently OFF): "
                    + ", ".join(missing_lines)
                    + ". The body parts those pieces would cover are bare."
                )
            if piece_names or missing_lines:
                out.append(line)

        body_lines = clothing_v2.compose_body_description_v2(char, entities)
        if body_lines and sig:
            # When a full-preset signature fired above, it already
            # carries the clothed-body prose. Suppress the per-part
            # Appearance lines for parts the preset covers — the
            # signature reads as a single cohesive paragraph instead
            # of being doubled by a per-part anatomy table the model
            # then copies template-wise. Uncovered parts (face, hands,
            # exposed skin) still surface here so the model keeps the
            # specifics it needs for proximity / expression detail.
            body_part_defs = props.get("body_parts") or {}
            body_lines = [
                (part, text) for part, text in body_lines
                if not (body_part_defs.get(part) or {}).get("covered", False)
            ]
        # Layer the accessories list (cat ears / tail / gloves / tattoo)
        # over the composed body — the v2 worn map doesn't include them.
        body_lines = _overlay_accessories_v2(char, entities, body_lines)
        if body_lines:
            # Per-part composition. Order already in body_parts insertion
            # order (or concise_order); just emit one line per part.
            body_text = "\n".join(
                _apply_outfit_template(text, outfit, char)
                for _, text in body_lines
            )
            body_text = apply_macros(body_text, ctx)
            out.append("Appearance:\n" + body_text)
        # Accessories summary + persistent body marks, same as the v1 path.
        acc_text = _accessories_line(char, entities)
        if acc_text:
            out.append(f"Accessories: {apply_macros(acc_text, ctx)}")
        marks_text = _body_marks_line(char, ctx)
        if marks_text:
            out.append(f"Body marks: {marks_text}")
    else:
        # v1 path (unchanged).
        if outfit_id and outfit_id in entities:
            outfit = _resolved_outfit(outfit_id, entities)
            # Compose accessories on top of the primary outfit's coverage.
            # Each accessory contributes its own per-part coverage (later
            # wins); accessories may also declare `displaces` to clear named
            # slots on the primary. Both text rendering (here) and the sprite
            # garment merge (api._resolve_sprite_state) read the composed
            # result so the prose and the image stay aligned.
            outfit = _compose_accessories(outfit, char, entities)
            outfit_props = outfit.get("properties") or {}
            outfit_text = (
                outfit_props.get("intact_description")
                or outfit_props.get("concise_description")
                or outfit.get("description")
                or outfit.get("name")
            )
            if outfit_text:
                # Templating: substitute {color} / {material} / {fit} / {style}
                # from character.properties.outfit_overrides (per-character
                # overlay) falling back to the outfit's own field. Lets a
                # generic outfit like bikini_generic carry the prose
                # "She wears a {color} two-piece bikini..." and the narrator
                # set `outfit_overrides.color = "gold"` on the character to
                # specialize without spinning up a whole new outfit instance.
                outfit_text = _apply_outfit_template(outfit_text, outfit, char)
                out.append(f"Currently wearing: {apply_macros(outfit_text, ctx)}")
            # Surface equipped accessories as a separate small line so the
            # model sees them distinctly from the primary outfit. Pulls each
            # accessory's concise text (templated).
            acc_text = _accessories_line(char, entities)
            if acc_text:
                out.append(f"Accessories: {apply_macros(acc_text, ctx)}")
            # Persistent body marks (tattoos, piercings) — surfaced as a
            # separate line so the model sees them as character-level marks
            # rather than outfit-level coverage. Each entry is a free-text
            # description keyed by body part.
            marks_text = _body_marks_line(char, ctx)
            if marks_text:
                out.append(f"Body marks: {marks_text}")

        # Surface any v1 slot "stages" — garments displaced OFF/PARTIAL
        # via clothing_overrides. v1 coverage is stateless, so these only
        # moved the sprite; without this the generating model never sees
        # that (e.g.) the shirt is off.
        state_line = _v1_clothing_state_line(char, outfit)
        if state_line:
            out.append(state_line)
        body_text = _body_description(char, outfit, ctx)
        if body_text:
            out.append("Appearance:\n" + body_text)

    equipped_text = _equipped_text(props.get("equipped"), entities, ctx)
    if equipped_text:
        out.append("Equipped:\n" + equipped_text)

    # Role overlay (set via user_personas_are_roles scenarios — Alex
    # playing "Federation liaison" rather than the user IS Federation
    # liaison). Renders as a small block after the body so the focal
    # AI reads "this is who they are normally" then "this is what role
    # they're filling right now."
    role_label = (char.get("role") or "").strip() if isinstance(char.get("role"), str) else ""
    role_desc = (char.get("role_description") or "").strip() if isinstance(char.get("role_description"), str) else ""
    if role_label or role_desc:
        bits = []
        if role_label:
            bits.append(role_label)
        if role_desc:
            bits.append(apply_macros(role_desc, ctx))
        out.append("Role in this scene: " + " — ".join(bits))

    rel_text = _relationships_text(props.get("relationships"), entities, ctx)
    if rel_text:
        out.append("Relationships:\n" + rel_text)

    if char.get("example_text"):
        out.append("Mood reference: " + apply_macros(char["example_text"], ctx).strip())

    extra_lines = _extra_state_lines(char, ctx)
    if extra_lines:
        out.append("Current state (narrator-tracked):\n" + "\n".join(extra_lines))

    return "\n\n".join(out)


# ---------------------------------------------------------------------------
# Extra state lines
#
# Anything the narrator writes onto a character entity via
# [set <character>.<dotted.path> = <value>] (or a fenced ```edits``` block)
# that isn't one of the standard fields rendered above shows up here. Lets
# the narrator carry persistent character state (power level, mood flags,
# inventory items, status effects, etc.) without requiring a schema change.
# Skip-lists below cover everything the standard render already handles
# plus internal plumbing (id / type / _template_id / etc.).
# ---------------------------------------------------------------------------

# ALLOWLISTS — this dump is default-DENY (see _extra_state_lines).
#
# History: it used to be default-OPEN (render everything not on a
# skip-list). Every field added to the character schema then leaked
# into the prompt until someone noticed and extended the skip-list —
# which happened twice (2026-05-29 appearance/emotional_map/boundaries;
# 2026-06-12 nine authored fields, ~71/86 characters affected, leaking
# author notes + entire variant bodies and squeezing the format
# primer out of context). Inverting to an allowlist makes a NEW
# authored field structurally unable to leak: unknown keys are simply
# not dumped. Real narrator-set state has no need of the catch-all —
# every key narrators actually write (notes, worn, body_parts,
# clothing_overrides, active_states) has its own dedicated renderer.

# Top-level entity keys that may surface as raw narrator state. Empty:
# narrators only ever patch under properties.*; top-level schema keys
# all render through dedicated paths.
_NARRATOR_STATE_TOP_LEVEL: frozenset[str] = frozenset()

# properties.* keys that may surface as raw narrator-set runtime state.
# These are generic status descriptors with NO dedicated card line —
# the documented purpose of the "Current state (narrator-tracked)"
# block (power level, mood flags, status effects). Everything else in
# properties.* is authored card schema or has its own renderer and is
# denied by default. `narrator_state` is the sanctioned dict namespace
# for anything new (rendered wholesale), so future runtime state never
# needs a code change here.
_NARRATOR_STATE_PROPERTIES = frozenset({
    "narrator_state", "mood", "status", "condition", "power_level",
    "effects", "status_effects", "flags",
    # `notes` is the open destination the narrator-add flow writes to
    # (`[set <char>.properties.notes.<key> = ...]` — status, relationships,
    # facts). It renders to OTHER characters via the cast-list renderer,
    # but without this the focal character never saw their OWN narrator
    # notes (their tipsy status, who their mother is, etc.). Surface it in
    # the focal's "Current state" so the edit actually reaches the model.
    "notes",
})


def _format_state_value(value: Any, indent: int = 0) -> str:
    """Render a JSON-ish value as a compact human-readable line/block."""
    pad = "  " * indent
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if not value:
            return "[]"
        return ", ".join(_format_state_value(v, indent + 1) for v in value)
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = [f"{pad}  {k}: {_format_state_value(v, indent + 1)}" for k, v in value.items()]
        return "\n" + "\n".join(lines)
    return str(value)


def _extra_state_lines(char: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
    """Surface narrator-set runtime state under "Current state
    (narrator-tracked)". Default-DENY: only allowlisted keys render, so
    authored card-schema fields can never leak (see the allowlist
    note above). Top-level and properties.* both honored."""
    lines: list[str] = []
    for k, v in (char or {}).items():
        if k not in _NARRATOR_STATE_TOP_LEVEL:
            continue
        rendered = _format_state_value(v)
        if isinstance(rendered, str):
            rendered = apply_macros(rendered, ctx) if isinstance(v, str) else rendered
        lines.append(f"- {k}: {rendered}")
    props = (char or {}).get("properties") or {}
    if isinstance(props, dict):
        for k, v in props.items():
            if k not in _NARRATOR_STATE_PROPERTIES:
                continue
            rendered = _format_state_value(v)
            if isinstance(v, str):
                rendered = apply_macros(rendered, ctx)
            lines.append(f"- {k}: {rendered}")
    return lines


def _relationships_text(
    relationships: Any,
    entities: dict[str, dict[str, Any]],
    ctx: dict[str, Any],
) -> str:
    """Format properties.relationships into bullet lines.

    Accepts:
      {"<target_id>": {"affinity": int, "trust": int, "respect": int, "notes": str}}
      {"<target_id>": "free-form string"}
    """
    if not isinstance(relationships, dict) or not relationships:
        return ""
    lines: list[str] = []
    for target, info in relationships.items():
        target_name = (entities.get(target) or {}).get("name") or target
        if isinstance(info, dict):
            bits: list[str] = []
            for k in ("affinity", "trust", "respect"):
                if k in info and info[k] is not None:
                    bits.append(f"{k} {info[k]}")
            note = info.get("notes") or info.get("note")
            if note:
                bits.append(f"\"{apply_macros(str(note), ctx)}\"")
            if bits:
                lines.append(f"- {target_name}: {', '.join(bits)}")
        elif isinstance(info, str) and info.strip():
            lines.append(f"- {target_name}: {apply_macros(info, ctx)}")
    return "\n".join(lines)


# Slot-based default nouns for the `Your <noun> is <state>` framing
# in `_right_now_block`. Each piece can override via
# `properties.noun`. The defaults are workable but generic; authors
# override for anything with a real name ("shirt" instead of "top",
# "yoga pants" instead of "bottom", "ribbon" instead of "headwear").
_SLOT_DEFAULT_NOUN: dict[str, str] = {
    "top":       "top",
    "bottom":    "bottom",
    "bra":       "bra",
    "underwear": "underwear",
    "phallus":   "undergarment",
    "pantyhose": "tights",
    "gloves":    "gloves",
    "legwear":   "socks",
    "shoes":     "shoes",
    "head":      "headwear",
    "face":      "face cover",
    "neck":      "necklace",
    "back":      "back piece",
    # overlay (tattoos/marks) intentionally skipped — overlays
    # aren't typically "your X is Y" addressable.
}

# Final-token plural nouns. The "Your X is/are Y" framing needs
# subject-verb agreement: "Your shirt IS rolled up", "Your pants
# ARE pulled down". Detection by last-word lookup against this
# set keeps the rule simple while covering the universally-plural
# clothing nouns. Authors with unusual plurals (`headphones`,
# `goggles`) extend by setting `properties.noun_plural: true`
# on the piece.
_PLURAL_NOUN_TOKENS: frozenset[str] = frozenset({
    "pants", "shorts", "panties", "briefs", "jeans", "leggings",
    "tights", "stockings", "socks", "shoes", "boots", "sneakers",
    "loafers", "sandals", "heels", "gloves", "pantyhose",
    "trousers", "slacks", "joggers", "sweats", "tops",
})


def _noun_for_piece(piece: dict[str, Any]) -> str:
    """Short noun the [Right now] framing addresses. Author's
    `properties.noun` wins; otherwise fall back to a slot-based
    default. Last resort is the slot id itself or the literal
    "item" when even the slot is missing."""
    props = piece.get("properties") or {}
    noun = props.get("noun")
    if isinstance(noun, str) and noun.strip():
        return noun.strip()
    slot = (props.get("slot") or "").lower()
    return _SLOT_DEFAULT_NOUN.get(slot) or slot or "item"


def _copula_for_noun(noun: str, piece: dict[str, Any] | None = None) -> str:
    """Return ``is`` or ``are`` for ``Your <noun> ...``. Author
    override via ``properties.noun_plural`` (bool) takes precedence;
    otherwise check the noun's last token against
    `_PLURAL_NOUN_TOKENS`. Defaults to singular when ambiguous."""
    if piece is not None:
        flag = (piece.get("properties") or {}).get("noun_plural")
        if isinstance(flag, bool):
            return "are" if flag else "is"
    tokens = noun.strip().split()
    if not tokens:
        return "is"
    return "are" if tokens[-1].lower() in _PLURAL_NOUN_TOKENS else "is"


def _state_display_label(piece: dict[str, Any], state_id: str) -> str:
    """Human-display version of a state id. ``properties.state_labels[
    state_id]`` overrides; default rule is snake_case → space. So
    `rolled_up` → "rolled up", `half_off` → "half off", `ripped`
    → "ripped"."""
    overrides = (piece.get("properties") or {}).get("state_labels") or {}
    if isinstance(overrides, dict):
        label = overrides.get(state_id)
        if isinstance(label, str) and label.strip():
            return label.strip()
    return (state_id or "").replace("_", " ").strip()


def _consequence_for_state(piece: dict[str, Any], state_id: str) -> str:
    """Extract a short consequence clause from the piece's
    ``coverage[state_id]`` data. Walks the coverage map in
    declaration order, picks the first entry with ``covered: true``,
    and produces a tail by splitting the description on the first
    em-dash or comma and returning the post-separator part. Returns
    "" when no usable description exists.

    Rationale: authored coverage descriptions for partial states
    follow a natural pattern — "<piece status>, <consequence>." or
    "<piece status> — <consequence>." Splitting on the separator
    yields the "what's now visible / how it sits" half, which
    matches what we want to surface in the declarative `Your X is
    Y — <consequence>` framing. The pre-separator half tends to
    re-state piece info we've already emitted ("The white t-shirt
    is rolled up high under the collarbones") so we drop it.

    Falls back to the full description (with trailing punctuation
    stripped) when no separator is present."""
    coverage = ((piece.get("properties") or {}).get("coverage") or {}).get(state_id) or {}
    if not isinstance(coverage, dict):
        return ""
    for _part, info in coverage.items():
        if not isinstance(info, dict):
            continue
        if not info.get("covered"):
            continue
        desc = (info.get("description") or "").strip()
        if not desc:
            continue
        # Prefer em-dash separators (cleanest split), then commas.
        for sep in (" — ", " - ", ", "):
            if sep in desc:
                _, _, tail = desc.partition(sep)
                tail = tail.rstrip(".!?").strip()
                if tail:
                    return tail
        return desc.rstrip(".!?").strip()
    return ""


def _right_now_block(
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    ctx: dict[str, Any],
) -> str:
    """Compose a tail-of-messages narrator-beat body describing
    any worn piece currently in a non-default state. Empty when
    nothing deviates or when the character isn't v2 (no worn map).

    Line shape: ``<Char>'s <noun> <is|are> <state-label> — <consequence>.``

    The caller wraps the joined body in ``Narrator: *Right now — <body>*``
    and injects as a user-role message at the tail of the messages
    array, matching the existing staging-beat shape that the rest
    of the conversation history uses.

    Third-person with the character's name resolves the misattribution
    we saw with second-person "Your" — when the previous user message
    was "*I casually enter Nadia's flat.*", "Your shirt is rolled up"
    parsed as the conversational addressee (user) rather than the
    system-prompt addressee (focal). Character-named third-person
    has no such ambiguity: "Nadia's shirt" is Nadia's shirt.

    Field sourcing:

    - ``<noun>`` comes from `_noun_for_piece` (piece.properties.noun
      with slot-based fallback). "shirt", "yoga pants", "suit",
      "cardigan", "ribbon".
    - ``<is|are>`` from `_copula_for_noun` — handles plural clothing
      nouns ("Nadia's pants ARE pulled down").
    - ``<state-label>`` from `_state_display_label` — the state id
      with underscores replaced by spaces, or an explicit label from
      `properties.state_labels`.
    - ``<consequence>`` from `_consequence_for_state` — a short
      what's-now-visible tail extracted from the piece's authored
      `coverage[state]` description. When no separator-split tail
      is available, the line collapses to "<Char>'s <noun> is <state>."

    Generic by design — composes entirely from the v2 piece schema
    (noun, states, coverage). Authoring per piece is one optional
    `noun` field plus the existing state ids; no per-state custom
    prose is required for the block to work. Same piece can be
    worn by any character; same framing applies, with the focal's
    name substituted at compose time.
    """
    props = char.get("properties") or {}
    worn = props.get("worn")
    if "worn" not in props or not isinstance(worn, dict) or not worn:
        return ""

    name = char.get("name") or char.get("id") or "She"
    # Possessive form. For names that already end in 's' (e.g. "Iris",
    # "Lukas") English style allows either "Iris'" or "Iris's"; we
    # default to "Iris's" — clearer for the model and matches the
    # name-with-apostrophe-s pattern the dialogue_pairs use.
    possessive = f"{name}'s"

    from . import clothing_v2

    lines: list[str] = []
    for slot in clothing_v2.LAYER_ORDER:
        entry = worn.get(slot) or {}
        if not isinstance(entry, dict):
            continue
        pid = entry.get("piece")
        state = entry.get("state")
        if not isinstance(pid, str) or not isinstance(state, str):
            continue
        # Off-state pieces are already flagged by the partial-state
        # anchor inside [You]'s wearing line ("NOT wearing (preset
        # items currently OFF): ..."). Skip here to avoid duplicate
        # surface.
        if state.lower() == "off":
            continue
        piece = clothing_v2._resolve_piece(pid, entities)
        if not piece:
            continue
        piece_props = piece.get("properties") or {}
        states = piece_props.get("states") or []
        default_state = states[0] if states else "on"
        if state == default_state:
            continue

        noun = _noun_for_piece(piece)
        copula = _copula_for_noun(noun, piece)
        label = _state_display_label(piece, state)
        tail = _consequence_for_state(piece, state)

        if tail:
            line = f"{possessive} {noun} {copula} {label} — {tail}."
        else:
            line = f"{possessive} {noun} {copula} {label}."
        lines.append(apply_macros(line, ctx))

    return " ".join(lines)


# Human phrasing for a v1 clothing_overrides displacement (a slot moved
# off its default ON state). PARTIAL = garment pushed aside / undone but
# still on; OFF = garment removed.
_V1_SLOT_STATE_PHRASE = {2: "partly undone / pushed aside", 3: "off / removed"}


def _v1_clothing_state_line(
    char: dict[str, Any], outfit: dict[str, Any] | None
) -> str:
    """One-line readout of garment slots displaced from their default ON
    state via ``clothing_overrides`` — the v1 sub-part "stages".

    v1's flat coverage map is stateless, so a slot the user or narrator
    set to PARTIAL/OFF (the Scene-staging slot picker, or a
    ``[set <char>.clothing_overrides.<slot> = 3]`` directive) moved only
    the SPRITE — the character prompt never learned the garment was
    displaced ("sub part stages don't read"). This surfaces it.

    Only slots the outfit normally has ON (base ``clothing_slots`` == 1)
    and that an override moved to PARTIAL/OFF are reported, so an outfit's
    inherent empty slots (a bra it never includes) aren't listed as
    "off". Returns "" when nothing is displaced. v2 characters use
    ``_right_now_block`` instead and never reach this.
    """
    props = char.get("properties") or {}
    overrides = props.get("clothing_overrides")
    if not isinstance(overrides, dict) or not overrides:
        return ""
    base = ((outfit or {}).get("properties") or {}).get("clothing_slots") or {}
    if not isinstance(base, dict):
        base = {}
    bits: list[str] = []
    for slot, val in overrides.items():
        if not isinstance(slot, str):
            continue
        try:
            n = int(val)
        except (TypeError, ValueError):
            continue
        if n not in (2, 3):
            continue
        try:
            base_n = int(base.get(slot.lower(), base.get(slot, 1)) or 1)
        except (TypeError, ValueError):
            base_n = 1
        if base_n != 1:
            continue  # outfit never had this slot on — not a displacement
        bits.append(f"{slot.lower().replace('_', ' ')} {_V1_SLOT_STATE_PHRASE[n]}")
    if not bits:
        return ""
    return "Clothing state (displaced right now): " + "; ".join(bits) + "."


def _body_description(
    char: dict[str, Any],
    outfit: dict[str, Any] | None,
    ctx: dict[str, Any],
) -> str:
    """Render an integrated body description.

    For each body part:
      - If the current outfit covers it AND has a description, use the
        outfit's coverage description (richer, mentions the garment).
      - Else if the outfit covers it and the body has a `clothed_base`,
        use that.
      - Else use the body's `base` (uncovered) description.

    Order follows properties.concise_order if defined; otherwise the
    body_parts dict insertion order.
    """
    props = char.get("properties") or {}
    body_parts = props.get("body_parts") or {}
    if not isinstance(body_parts, dict) or not body_parts:
        return ""

    coverage = ((outfit or {}).get("properties") or {}).get("coverage") or {}
    if not isinstance(coverage, dict):
        coverage = {}

    # Is the character dressed in a real outfit (not one of
    # _NUDE_OUTFIT_IDS)? Used below to decide the fail direction for
    # parts whose coverage is an unauthored stub: a dressed character
    # must never be rendered undressed for a part the outfit failed to
    # describe.
    outfit_id = outfit.get("id") if isinstance(outfit, dict) else None
    outfit_dressed = outfit is not None and outfit_id not in _NUDE_OUTFIT_IDS

    order = props.get("concise_order") or list(body_parts.keys())
    seen: set[str] = set()
    full_order: list[str] = []
    for k in order:
        if k in body_parts and k not in seen:
            full_order.append(k); seen.add(k)
    for k in body_parts:
        if k not in seen:
            full_order.append(k); seen.add(k)

    lines: list[str] = []
    for part in full_order:
        bp = body_parts.get(part) or {}
        cov = coverage.get(part) or {}
        # An empty-description `covered: False` entry is an unauthored
        # STUB, not a statement of coverage. Every well-authored outfit —
        # including minimal ones — carries a real
        # description on each uncovered part; an outfit whose coverage is all
        # empty stubs (e.g. a `*_default` outfit that names garments in
        # its `intact_description` but never filled the per-part map)
        # otherwise renders the character fully undressed despite the
        # "Currently wearing" line — the reported "outfits read as undressed"
        # bug. On a dressed character, don't assert an uncovered
        # body for a stubbed part: use `clothed_base` if authored,
        # else omit the part and let the outfit description carry it.
        if (
            outfit_dressed
            and cov.get("covered") is False
            and not str(cov.get("description") or "").strip()
        ):
            clothed = (bp.get("clothed_base") or "").strip()
            if clothed:
                clothed = _apply_outfit_template(clothed, outfit, char)
                mark_extra = _body_mark_for_part(char, part)
                if mark_extra:
                    clothed = f"{clothed}; {mark_extra}".strip("; ")
                clothed = apply_macros(clothed, ctx).strip()
                if clothed:
                    lines.append(f"- {part.replace('_', ' ').capitalize()}: {clothed}")
            continue
        # Decide visibility. Outfit coverage wins when present; else fall back
        # to the body's own `covered` flag.
        is_covered = cov.get("covered") if "covered" in cov else bp.get("covered", False)
        if is_covered:
            opacity = (cov.get("opacity") or "opaque").lower()
            cov_desc = cov.get("description") or bp.get("clothed_base") or bp.get("base") or ""
            base_desc = bp.get("base") or ""
            reveals = cov.get("reveals")
            if reveals:
                # Author-supplied override wins.
                text = f"{cov_desc} — {reveals}".strip(" —")
            elif opacity == "sheer" and base_desc and cov_desc:
                # Sheer fabric: render as structured "garment; underneath:
                # body" rather than the prose template "with X visible
                # through the thin fabric" — the prose template was the
                # documented chest-fabric tic source (prompt_anatomy.md
                # §5). Field-label form gives the model the same two
                # facts (garment + body underneath) without supplying a
                # ready-made sentence shape to copy verbatim.
                text = f"{cov_desc}; underneath: {base_desc}"
            elif opacity == "transparent" and base_desc and cov_desc:
                # See-through: same structural form as sheer with the
                # opacity called out as field metadata.
                text = f"{cov_desc}; fully visible underneath: {base_desc}"
            else:
                text = cov_desc
        else:
            text = bp.get("base") or ""
            # Accessory contributions on an otherwise-uncovered part are
            # additive — append the accessory's description to the base
            # text instead of dropping it. Lets cat ears, a tail rising
            # from the lower back, etc. surface in the rendered prose. The
            # _from_accessory tag is set by _compose_accessories.
            cov_desc = cov.get("description")
            if cov.get("_from_accessory") and isinstance(cov_desc, str) and cov_desc.strip():
                text = f"{text}; {cov_desc}".strip("; ")
        # Apply outfit-templating placeholders ({color}, {material}, etc.)
        # before the macro pass so per-character outfit_overrides flow
        # through to per-part coverage prose (a "gold bikini" surfaces in
        # every covered slot, not just the top-level description line).
        text = _apply_outfit_template(text, outfit, char)
        # Append body-marks for this part (persistent tattoos / piercings)
        # — these stay through outfit changes, so the appearance line
        # reflects them even when the part is also covered by an outfit.
        mark_extra = _body_mark_for_part(char, part)
        if mark_extra:
            text = f"{text}; {mark_extra}".strip("; ")
        text = apply_macros(text, ctx).strip()
        if text:
            lines.append(f"- {part.replace('_', ' ').capitalize()}: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dynamic dialogue pairs
#
# Beyond the character's flat `dialogue_pairs` base set, two layers add
# context-relevant primer pairs at assembly time, gated against the live
# scene the same declarative way the prompt registry gates blocks:
#
#   Layer A — per-character `properties.conditional_pairs`:
#       [{ "when": {<selector>}, "pairs": [{user,char}, ...] }]
#   Layer B — global `data/pairs/<id>.json` (type "pair_set"):
#       { "properties": { "when": {<selector>}, "pairs": [...],
#                         "require_char_traits"?: [...],
#                         "require_char_tags"?: [...] } }
#     Macro-generic ({{char}}/{{user}}); fires for any focal whose
#     context matches `when` and who meets the char requirements.
#
# Selector keys (all present keys must match — AND):
#   room_tag, room, location, user_role (substring), focal_nude (bool),
#   focal_tag.
#
# Cap discipline: context pairs are capped and DISPLACE base pairs rather
# than piling on, so a context-heavy scene doesn't blow num_ctx. Context
# pairs render LAST (nearest the live scene = highest attention).
# ---------------------------------------------------------------------------

PAIR_TOTAL_CAP = 22
PAIR_CONTEXT_CAP = 6
_NUDE_OUTFIT_IDS = {"nude", "nude_female_generic", "nude_male"}

# Canonical relationship-tag vocabulary — the generic, module-free words a
# character's `conditional_pairs` gate on via `when: {rel_tag: "..."}`, and
# the words a relationship snapshot stores. Deliberately generic RP terms (no
# Pathfinder "attitude" ladder, no module concepts): a character authored with
# these buckets works in ANY scenario, and whatever is driving relationships
# (a scenario seed, a plain in-fiction event, or an optional module projecting
# its own state) maps onto the SAME words. Authors may add their own tags too;
# these are just the set automated writers target and the set a bare
# user_role can seed from.
REL_VOCAB = {
    "stranger", "acquaintance", "friend", "close",
    "crush", "lover", "wary", "hostile",
}

# "Cold" standings — the focal barely knows / distrusts the user. When one is
# active, the character's warm/familiar base pairs actively contradict the
# scene (a card whose base pool assumes intimacy would still show the model
# kissing/bedroom pairs to someone the focal just met), so we hard-cap the base
# pool to a few pairs and let the cold context bucket dominate. The context
# pairs still render LAST (highest attention). Tunable knob.
COLD_STANDINGS = {"stranger", "wary", "hostile"}
COLD_BASE_CAP = 3


def resolve_relationship_tags(
    focal_id: str | None,
    conversation: dict[str, Any] | None,
    user_role: str,
) -> set[str]:
    """The focal's relationship tags toward the user for this turn.

    Resolution order:
      1. An explicit branch-local `metadata.relationships` snapshot on the
         active path (written by a scenario seed / in-fiction event / optional
         module) — per-character, evolving, and Return-by-Death-safe.
      2. Otherwise SEED from the user's role: if the active user persona's role
         is itself a relationship word (e.g. a "stranger" persona), that's the
         starting standing until something writes an explicit snapshot.
      3. Otherwise empty — no relationship gating, base pool carries the scene.

    Core-only: reads its own generic slot; never imports or knows about any
    module."""
    if conversation and focal_id:
        try:
            from .effective import relationships_snapshot_for_path
            snap = relationships_snapshot_for_path(conversation, focal_id, "user")
        except Exception:
            snap = None
        if snap is not None:
            return {str(t).lower() for t in snap}
    r = (user_role or "").strip().lower()
    return {r} if r in REL_VOCAB else set()


def perceives_user_identity(
    focal_id: str | None,
    conversation: dict[str, Any] | None,
    user_role: str,
) -> bool:
    """Does this focal KNOW who the user is — their name/identity?

    1. An explicit branch-local acquaintance fact wins (an introduction set it,
       or a rewind cleared it).
    2. Otherwise derive from standing: acquainted UNLESS the standing is cold
       (stranger / wary / hostile). This keeps every established scenario
       working (non-cold → known) while a stranger scene hides the name.

    Blocking identity is separate from locational memory (which blocks
    unwitnessed history); this blocks unlearned identity."""
    if conversation and focal_id:
        try:
            from .effective import acquaintance_for_path
            fact = acquaintance_for_path(conversation, focal_id, "user")
        except Exception:
            fact = None
        if fact is not None:
            return bool(fact)
    return not (resolve_relationship_tags(focal_id, conversation, user_role) & COLD_STANDINGS)


def user_descriptor(user_persona: dict[str, Any] | None) -> str:
    """What a focal who doesn't know the user calls them: an authored
    `stranger_label`, else the persona's role, else 'a stranger'."""
    up = user_persona or {}
    label = (up.get("stranger_label") or "").strip() if isinstance(up.get("stranger_label"), str) else ""
    if label:
        return label
    role = (up.get("role") or "").strip() if isinstance(up.get("role"), str) else ""
    if role:
        role_l = role.lower()
        return role if role_l.startswith(("a ", "an ", "the ")) else f"a {role}"
    return "a stranger"


def _entity_or_template(eid: str, entities: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve an entity from the branch's instanced entities, falling back
    to the global template catalog (outfits/scenarios aren't always
    instanced)."""
    ent = entities.get(eid)
    if isinstance(ent, dict):
        return ent
    try:
        from . import entities as _ent_mod
        t = _ent_mod.get(eid)
        return t if isinstance(t, dict) else None
    except Exception:
        return None


def _pair_context(
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]] | None,
    presence: dict[str, Any] | None,
    settings: dict[str, Any] | None,
    conversation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the live-scene facts the pair selectors gate on: the focal's
    current room (+ its tags) and location, the user's role, whether the
    focal has no outfit on, the focal's tags/traits, the tags of the outfit the
    focal is wearing, and the active scenario's tags.

    The outfit + scenario tags are the hook for keeping pair SETS generic
    while still targeting specifics: a generic set fires off
    `outfit_tag`/`scenario_tag`, and the tagging lives on the (Peach-
    specific) clothing/scenario assets, not in the pair set."""
    presence = presence or {}
    entities = entities or {}
    room_id = presence.get("room")
    room = entities.get(room_id) if room_id else None
    room_tags = {str(t).lower() for t in ((room or {}).get("tags") or [])}
    user_persona = (settings or {}).get("user_persona") or {}
    user_role = (user_persona.get("role") or "").strip().lower()
    props = char.get("properties") or {}
    worn = props.get("worn")
    cur = props.get("current_outfit") or presence.get("outfit")
    focal_nude = (isinstance(worn, dict) and not worn) or (
        isinstance(cur, str) and cur in _NUDE_OUTFIT_IDS
    )

    # Outfit tags — the current_outfit bundle's tags + every worn piece's
    # tags, so a set can match either the preset (e.g. "formal") or a piece.
    outfit_tags: set[str] = set()
    if isinstance(cur, str) and cur:
        bundle = _entity_or_template(cur, entities)
        if bundle:
            outfit_tags.update(str(t).lower() for t in (bundle.get("tags") or []))
    if isinstance(worn, dict):
        for entry in worn.values():
            pid = entry.get("piece") if isinstance(entry, dict) else None
            if isinstance(pid, str) and pid:
                piece = _entity_or_template(pid, entities)
                if piece:
                    outfit_tags.update(str(t).lower() for t in (piece.get("tags") or []))

    # Scenario tags — from the conversation's scenario_id.
    scenario_tags: set[str] = set()
    sid = (conversation or {}).get("scenario_id")
    if isinstance(sid, str) and sid:
        scen = _entity_or_template(sid, entities)
        if scen:
            scenario_tags.update(str(t).lower() for t in (scen.get("tags") or []))

    # User-persona tags — editable in the GUI (the "Your persona" panel)
    # so the user can tag who they're playing and have matching pairs
    # surface. Stored on the user entity's `persona_tags` (branch-scoped),
    # with the settings.user_persona mirror as fallback. Distinct from the
    # entity's `tags` (which holds the reserved "user" marker).
    user_tags: set[str] = set()
    user_ent = entities.get("user") or {}
    user_tags.update(str(t).lower() for t in (user_ent.get("persona_tags") or []))
    user_tags.update(str(t).lower() for t in (user_persona.get("tags") or []))

    # Object tags — the tags of every object present in the active scene, so
    # a pair set (or, via the image-pick path, an image pack) can surface
    # from "an object is in the room" the same way it surfaces from a room or
    # outfit tag. Read defensively from the conversation's active-leaf
    # presence snapshot; any shape surprise degrades to no object tags rather
    # than raising.
    object_tags: set[str] = set()
    try:
        conv = conversation or {}
        msgs = conv.get("messages") or {}
        leaf = msgs.get(conv.get("active_path_leaf") or "") or {}
        # Objects in scene = the branch's cast objects (path-replayed
        # cast_add/cast_remove) plus any snapshot objects_present
        # (scenarios seeding starting_state.objects_present).
        present = set((leaf.get("presence_snapshot") or {}).get("objects_present") or {})
        if conv.get("id"):
            from .effective import effective_cast_at
            present |= effective_cast_at(conv).get("objects") or set()
        for obj_id in present:
            obj = _entity_or_template(obj_id, entities)
            if obj:
                object_tags.update(str(t).lower() for t in (obj.get("tags") or []))
    except Exception:
        object_tags = set()

    return {
        "room_id": room_id,
        "room_tags": room_tags,
        "location_id": presence.get("location"),
        "user_role": user_role,
        "user_tags": user_tags,
        "rel_tags": resolve_relationship_tags(char.get("id"), conversation, user_role),
        "focal_nude": bool(focal_nude),
        "focal_tags": {str(t).lower() for t in (char.get("tags") or [])},
        # personality may be a {trait: weight} dict or free-text prose (many
        # hand-authored cards use a string) — only a dict yields trait keys.
        "focal_traits": (lambda p: set(p.keys()) if isinstance(p, dict) else set())(props.get("personality")),
        "outfit_tags": outfit_tags,
        "scenario_tags": scenario_tags,
        "object_tags": object_tags,
    }


def _tag_match(want: Any, have: set[str]) -> bool:
    """A selector tag value may be a single tag or a list ('any of'). True
    iff at least one wanted tag is present in `have`."""
    wants = want if isinstance(want, list) else [want]
    return bool({str(w).lower() for w in wants} & have)


def _when_matches(when: Any, c: dict[str, Any]) -> bool:
    """True iff every key in the `when` selector matches the context `c`.
    Tag-type keys (room_tag / focal_tag / outfit_tag / scenario_tag) accept
    a single tag or a list (any-of)."""
    if not isinstance(when, dict) or not when:
        return False
    if "room_tag" in when and not _tag_match(when["room_tag"], c["room_tags"]):
        return False
    if "focal_tag" in when and not _tag_match(when["focal_tag"], c["focal_tags"]):
        return False
    if "outfit_tag" in when and not _tag_match(when["outfit_tag"], c["outfit_tags"]):
        return False
    if "scenario_tag" in when and not _tag_match(when["scenario_tag"], c["scenario_tags"]):
        return False
    if "object_tag" in when and not _tag_match(when["object_tag"], c.get("object_tags") or set()):
        return False
    if "user_tag" in when and not _tag_match(when["user_tag"], c["user_tags"]):
        return False
    if "rel_tag" in when and not _tag_match(when["rel_tag"], c.get("rel_tags") or set()):
        return False
    if "room" in when and when["room"] != c["room_id"]:
        return False
    if "location" in when and when["location"] != c["location_id"]:
        return False
    if "user_role" in when and str(when["user_role"]).lower() not in c["user_role"]:
        return False
    if "focal_nude" in when and bool(when["focal_nude"]) != c["focal_nude"]:
        return False
    return True


def _collect_context_pairs(char: dict[str, Any], c: dict[str, Any]) -> list[dict[str, Any]]:
    """Gather Layer-A (per-character) + Layer-B (global) pairs whose
    selectors match the context. Global sets may additionally require the
    focal to carry a trait/tag (so e.g. a public-nudity-awkward set only
    fires for bashful characters, not a confident one)."""
    out: list[dict[str, Any]] = []
    for grp in (char.get("properties") or {}).get("conditional_pairs") or []:
        if isinstance(grp, dict) and _when_matches(grp.get("when"), c):
            out.extend(p for p in (grp.get("pairs") or []) if isinstance(p, dict))
    try:
        from . import entities as _ent
        sets = _ent.by_type("pair_set")
    except Exception:
        sets = []
    for s in sorted(sets, key=lambda e: e.get("id") or ""):
        sp = s.get("properties") or {}
        if not _when_matches(sp.get("when"), c):
            continue
        req_traits = sp.get("require_char_traits") or []
        req_tags = sp.get("require_char_tags") or []
        if req_traits and not any(t in c["focal_traits"] for t in req_traits):
            continue
        if req_tags and not any(t in c["focal_tags"] for t in req_tags):
            continue
        out.extend(p for p in (sp.get("pairs") or []) if isinstance(p, dict))
    return out


def _pair_selection_report(char: dict[str, Any], c: dict[str, Any] | None) -> str | None:
    """Dev-panel readout: the live context tags + which dialogue-pair sets
    fired for this focal this turn. Pure introspection — returned as a
    `pieces` entry, never part of system_text. Mirrors the matching logic
    in `_collect_context_pairs` so what it reports is what actually fired."""
    if c is None:
        return None

    def _fmt(s: Any) -> str:
        return ", ".join(sorted(s)) if s else "—"

    lines = [
        "Active context:",
        f"  room={c.get('room_id') or '—'}  room_tags=[{_fmt(c['room_tags'])}]  location={c.get('location_id') or '—'}",
        f"  outfit_tags=[{_fmt(c['outfit_tags'])}]",
        f"  scenario_tags=[{_fmt(c['scenario_tags'])}]",
        f"  focal_tags=[{_fmt(c['focal_tags'])}]  focal_nude={c['focal_nude']}",
        f"  user_role={c.get('user_role') or '—'}  user_tags=[{_fmt(c['user_tags'])}]",
        f"  rel_tags=[{_fmt(c.get('rel_tags') or set())}]",
    ]

    fired: list[str] = []
    for i, grp in enumerate((char.get("properties") or {}).get("conditional_pairs") or []):
        if isinstance(grp, dict) and _when_matches(grp.get("when"), c):
            n = len([p for p in (grp.get("pairs") or []) if isinstance(p, dict)])
            fired.append(f"Layer-A group #{i} {grp.get('when')} (+{n})")
    try:
        from . import entities as _ent
        sets = _ent.by_type("pair_set")
    except Exception:
        sets = []
    for s in sorted(sets, key=lambda e: e.get("id") or ""):
        sp = s.get("properties") or {}
        if not _when_matches(sp.get("when"), c):
            continue
        req_traits = sp.get("require_char_traits") or []
        req_tags = sp.get("require_char_tags") or []
        if req_traits and not any(t in c["focal_traits"] for t in req_traits):
            continue
        if req_tags and not any(t in c["focal_tags"] for t in req_tags):
            continue
        n = len([p for p in (sp.get("pairs") or []) if isinstance(p, dict)])
        fired.append(f"global '{s.get('id')}' {sp.get('when')} (+{n})")

    base_n = len([p for p in ((char.get("properties") or {}).get("dialogue_pairs") or []) if isinstance(p, dict)])
    added = min(sum(int(f.rsplit("+", 1)[1].rstrip(")")) for f in fired), PAIR_CONTEXT_CAP) if fired else 0
    kept_base = max(0, PAIR_TOTAL_CAP - added)
    lines.append(
        "Fired pair sets: " + ("; ".join(fired) if fired else "none")
    )
    lines.append(
        f"Primer budget: base {min(base_n, kept_base)}/{base_n} kept + {added} context "
        f"(cap {PAIR_TOTAL_CAP}, context cap {PAIR_CONTEXT_CAP})"
    )
    return "\n".join(lines)


def _example_messages(
    char: dict[str, Any],
    ctx: dict[str, Any],
    pair_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Convert dialogue_pairs (preferred) or dialogue_examples (legacy)
    into primer turns bracketed by *** separators.

    `dialogue_pairs`: list of {"user": "...", "char": "..."} dicts. Each
    pair becomes a user → assistant turn so the model sees the actual
    conversation shape — varied user inputs and varied char replies.

    `dialogue_examples` (legacy): flat list of char-only reply strings.
    Each becomes one assistant turn; bare strings get wrapped in quotes,
    rich strings (containing * or ") render verbatim.
    """
    name = char.get("name") or char.get("id")
    user_name = ctx.get("user_name") or "User"
    props = char.get("properties") or {}

    pairs = props.get("dialogue_pairs")
    if isinstance(pairs, list) and pairs:
        # Merge base pairs with any context-relevant pairs (Layers A + B).
        # Context pairs are capped and displace base pairs (swap, not pile
        # on) so the primer stays within budget, and render LAST.
        base = [p for p in pairs if isinstance(p, dict)]
        context_pairs = (
            _collect_context_pairs(char, pair_context)[:PAIR_CONTEXT_CAP]
            if pair_context is not None else []
        )
        keep_base = max(0, PAIR_TOTAL_CAP - len(context_pairs))
        # Cold standing + a context bucket that fired: suppress most of the warm
        # base pool so the cold pairs aren't drowned by familiar/intimate ones.
        if context_pairs and pair_context and (
            (pair_context.get("rel_tags") or set()) & COLD_STANDINGS
        ):
            keep_base = min(keep_base, COLD_BASE_CAP)
        selected = base[:keep_base] + context_pairs

        msgs: list[dict[str, str]] = [
            {"role": "system", "content": f"{EXAMPLE_SEPARATOR} Example dialogue for {name} (not real chat) {EXAMPLE_SEPARATOR}"},
        ]
        for p in selected:
            if not isinstance(p, dict):
                continue
            u = apply_macros((p.get("user") or "").strip(), ctx)
            c = apply_macros((p.get("char") or p.get("response") or "").strip(), ctx)
            if not u or not c:
                continue
            # Speaker labels deliberately omitted from primer turns. With
            # them, every primer assistant message read "<name>: <body>",
            # which trained the live model to emit "<name>: " at the start
            # of every new reply. The strip layer (_strip_speaker_prefix +
            # the streaming full_prefix buffer in routes/stream.py) catches
            # the exact-match canonical form at start, but missed common
            # variants ("**Iris**:", "IRIS:", "Iris :", the prefix
            # appearing after an opening *asterisk* block) — the user-
            # facing symptom is a leaked "Iris:" mid-message. Without
            # the primer labels the model has nothing to imitate; the
            # asterisks-and-quotes inside each primer body give it enough
            # format signal.
            msgs.append({"role": "user", "content": u})
            if '"' in c or '*' in c:
                msgs.append({"role": "assistant", "content": c})
            else:
                msgs.append({"role": "assistant", "content": f'"{c}"'})
        if len(msgs) < 2:
            return []
        msgs.append({"role": "system", "content": f"===== END OF EXAMPLES — REFERENCE ONLY, DO NOT COPY THE ABOVE. THE LIVE SCENE STARTS BELOW. ====="})
        return msgs

    raw_examples = props.get("dialogue_examples") or []
    if not isinstance(raw_examples, list):
        return []
    examples = [apply_macros(x, ctx).strip() for x in raw_examples[:6] if isinstance(x, str)]
    if not examples:
        return []
    msgs = [
        {"role": "system", "content": f"{EXAMPLE_SEPARATOR} Example dialogue for {name} (not real chat) {EXAMPLE_SEPARATOR}"},
    ]
    for line in examples:
        # No speaker label — same rationale as the dialogue_pairs path
        # above.
        if '"' in line or '*' in line:
            content = line
        else:
            content = f'"{line}"'
        msgs.append({"role": "assistant", "content": content})
    msgs.append({"role": "system", "content": f"===== END OF EXAMPLES — REFERENCE ONLY, DO NOT COPY THE ABOVE. THE LIVE SCENE STARTS BELOW. ====="})
    return msgs


_OUTFIT_TEMPLATE_PLACEHOLDERS = ("color", "material", "fit", "style")
_OUTFIT_TEMPLATE_RE = re.compile(
    r"\s*\{(" + "|".join(_OUTFIT_TEMPLATE_PLACEHOLDERS) + r")\}\s*",
    re.IGNORECASE,
)
_WHITESPACE_COLLAPSE = re.compile(r"\s{2,}")


def _apply_outfit_template(
    text: str,
    outfit: dict[str, Any] | None,
    char: dict[str, Any] | None,
) -> str:
    """Substitute ``{color}`` / ``{material}`` / ``{fit}`` / ``{style}``
    placeholders in `text` using the outfit's resolved fields, overridden
    by the character's ``properties.outfit_overrides.<key>`` overlay.

    Lets a generic outfit carry templated prose like
    ``"She wears a {color} knit cardigan..."`` and the narrator
    specialize per-character with
    ``[set iris.properties.outfit_overrides.color = "gold"]`` instead of
    spinning up a whole new outfit instance. When the placeholder
    resolves to an empty string the surrounding whitespace is squashed,
    so an un-set color collapses ``"a {color} cardigan"`` to ``"a cardigan"``
    rather than leaving a double space.
    """
    if not text or not isinstance(text, str):
        return text or ""
    outfit_props = (outfit or {}).get("properties") or {}
    char_props = (char or {}).get("properties") or {}
    overrides = char_props.get("outfit_overrides") or {}
    if not isinstance(overrides, dict):
        overrides = {}

    def _sub(m: re.Match[str]) -> str:
        key = (m.group(1) or "").lower()
        val = overrides.get(key) or outfit_props.get(key) or ""
        if not isinstance(val, str):
            val = str(val)
        val = val.strip()
        if not val:
            return " "
        # Preserve a single leading/trailing space so the substitution
        # doesn't fuse the word into adjacent text.
        return f" {val} "

    out = _OUTFIT_TEMPLATE_RE.sub(_sub, text)
    out = _WHITESPACE_COLLAPSE.sub(" ", out)
    return out.strip()


def _overlay_accessories_v2(
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    body_lines: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Overlay a v2 character's ``properties.accessories`` list onto the
    already-composed per-part body lines.

    The v2 path (``compose_body_description_v2``) only reads the ``worn``
    map, so accessories — which live in a separate list and layer over
    whatever is worn — were dropped. This mirrors the v1
    ``_compose_accessories`` merge, but onto the composed lines:

      * additive accessory (coverage entry ``covered: false`` + a
        description — cat ears on the head, a tail at the lower back, a
        tattoo on a shoulder): append its prose to that part's line.
      * occluding accessory (``covered: true`` — cat-paw gloves over
        arms/hands): replace that part's line.
      * ``under: true`` accessory: contributes no visible prose.

    Uses the accessory outfit's own authored coverage prose, templated
    with the character's ``{color}`` overrides.
    """
    props = char.get("properties") or {}
    acc_ids = props.get("accessories") or []
    if not isinstance(acc_ids, list) or not acc_ids:
        return body_lines
    order = [p for p, _ in body_lines]
    text = {p: t for p, t in body_lines}

    # Parts a worn garment OCCLUDES (covered:true, not revealing). An
    # additive accessory (a tattoo/piercing on bare skin) must NOT be
    # stapled onto such a part — that produces "the panel covers her
    # arm … her bare arm" contradictions, and re-introduces a
    # signature-suppressed covered part. Occlusion is read from the worn
    # map (independent of body_lines), so it holds even when the
    # signature stripped the covered part out.
    from . import clothing_v2
    occluded: set[str] = set()
    worn = props.get("worn") or {}
    if isinstance(worn, dict):
        for _slot, w in worn.items():
            if not isinstance(w, dict) or not w.get("piece"):
                continue
            piece = clothing_v2._resolve_piece(w.get("piece"), entities)
            if not piece:
                continue
            pstates = (piece.get("properties") or {}).get("states") or ["on"]
            st = w.get("state") or (pstates[0] if pstates else "on")
            cov = ((piece.get("properties") or {}).get("coverage") or {}).get(st) or {}
            for part, ce in cov.items():
                if isinstance(ce, dict) and ce.get("covered") and not ce.get("revealing"):
                    occluded.add(part)

    for acc_id in acc_ids:
        if not isinstance(acc_id, str):
            continue
        acc = _resolved_outfit(acc_id, entities)
        if not acc:
            g = ent_mod.get(acc_id)
            acc = g if isinstance(g, dict) and g.get("type") == "outfit" else None
        if not acc:
            continue
        acc_props = acc.get("properties") or {}
        if acc_props.get("under"):
            continue
        cov = acc_props.get("coverage") or {}
        if not isinstance(cov, dict):
            continue
        for part, entry in cov.items():
            if not isinstance(entry, dict):
                continue
            desc = _apply_outfit_template((entry.get("description") or "").strip(), acc, char)
            if not desc:
                continue
            if entry.get("covered"):
                # Occluding accessory (cat-paw gloves) — replaces the part.
                if part not in text:
                    order.append(part)
                text[part] = desc
            elif part in occluded:
                # Additive accessory on a part a worn garment occludes —
                # not visible under the clothing; skip it.
                continue
            elif text.get(part):
                cur = text[part].rstrip()
                if desc not in cur:
                    if cur and cur[-1] not in ".!?":
                        cur += "."
                    text[part] = cur + " " + desc
            else:
                order.append(part)
                text[part] = desc
    return [(p, text[p]) for p in order if text.get(p)]


def _compose_accessories(
    primary_outfit: dict[str, Any] | None,
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Compose a character's ``properties.accessories`` list onto the
    primary outfit's coverage, clothing_slots, and garments maps.

    Returns a NEW outfit dict (deep-copy) when accessories are present;
    returns ``primary_outfit`` unchanged when no accessories list is set.
    Each accessory is itself an outfit-shape entity resolved through the
    standard ``_resolved_outfit`` extends chain. Composition order is
    the order of the accessories list; later wins per body part.

    An accessory may declare ``properties.displaces: ["gloves"]`` to
    clear those clothing_slots on the primary (sets state 3 = off)
    before contributing its own slot data. Lets cat_gloves displace a
    primary outfit's gloves cleanly, for example.

    Text rendering (``_body_description``) reads the composed coverage
    map. Sprite rendering (``api._resolve_sprite_state``) reads the
    composed clothing_slots + garments maps so accessory garments
    actually surface in the image when the per-character wardrobe has
    the asset authored.
    """
    char_props = char.get("properties") or {}
    accessory_ids = char_props.get("accessories") or []
    if not isinstance(accessory_ids, list) or not accessory_ids:
        return primary_outfit
    if not primary_outfit:
        # Build a bare host so accessories have something to merge onto.
        primary_outfit = {"properties": {}}

    composed = copy.deepcopy(primary_outfit)
    composed_props = composed.setdefault("properties", {})
    composed_coverage = dict(composed_props.get("coverage") or {})
    composed_slots = dict(composed_props.get("clothing_slots") or {})
    composed_garments = dict(composed_props.get("garments") or {})

    for acc_id in accessory_ids:
        if not isinstance(acc_id, str):
            continue
        acc = _resolved_outfit(acc_id, entities)
        if not acc:
            # Fall back to the global template catalog so accessory
            # outfits referenced in the list resolve even when they
            # haven't been instanced into the conversation yet.
            global_tmpl = ent_mod.get(acc_id)
            if isinstance(global_tmpl, dict) and global_tmpl.get("type") == "outfit":
                acc = global_tmpl
        if not acc:
            continue
        acc_props = acc.get("properties") or {}

        # Layer model. `under: true` declares this piece is an
        # under-garment worn BENEATH the primary outfit (gold bikini
        # under a school uniform, sports bra under a t-shirt, etc.).
        # Under-garments contribute slot occupation + garment picks
        # (so the bra slot reads as "bikini bra" for sprite rendering)
        # but do NOT override the primary's body-part coverage
        # descriptions — the visible body text stays "shirt + vest +
        # blazer covering chest", not "bikini top covering chest".
        # `under: false` (the default) is the over-layer behavior —
        # visible accessories like cat ears, cat gloves, tail plug,
        # etc. — whose coverage descriptions DO win over the primary.
        # Under-garments also only contribute slot states they
        # actively fill (state 1); their off-state (3) slot
        # declarations are ignored so they don't strip the primary's
        # outerwear when used as a base layer.
        is_under = bool(acc_props.get("under"))

        # `displaces`: clear named slots on the primary before merging
        # this accessory's own slot data. Lets an accessory remove a
        # piece of the primary (e.g., cat_gloves displaces the
        # primary's gloves cleanly).
        for displaced_slot in (acc_props.get("displaces") or []):
            if isinstance(displaced_slot, str):
                composed_slots[displaced_slot] = 3

        # Per-part coverage merge — over-layer wins on shared parts.
        # Under-layers skip this entirely so the primary's visible
        # description survives. Marked `_from_accessory: true` so the
        # body-description renderer can distinguish "accessory adds a
        # detail to an otherwise-bare part" (cat ears on the head,
        # a tail at the lower back) from "garment covers the part"
        # (a shirt over the torso). For covered=true the accessory
        # replaces the part wholesale; for covered=false the renderer
        # appends the description to the body's base text instead of
        # silently dropping it.
        if not is_under:
            acc_coverage = acc_props.get("coverage") or {}
            if isinstance(acc_coverage, dict):
                for part, cov in acc_coverage.items():
                    if isinstance(cov, dict):
                        tagged = dict(cov)
                        tagged["_from_accessory"] = True
                        composed_coverage[part] = tagged

        # Slot state merge. Over-layers (cat ears style) win on every
        # slot they declare. Under-layers only contribute on-state
        # (1) slots — their off-state declarations don't strip the
        # primary's outerwear (`bikini.clothing_slots.top = 3` would
        # otherwise turn off the uniform shirt when bikini is worn as
        # an under-garment).
        acc_slots = acc_props.get("clothing_slots") or {}
        if isinstance(acc_slots, dict):
            for slot, state in acc_slots.items():
                if not isinstance(state, (int, float)):
                    continue
                n = int(state)
                if is_under and n != 1:
                    continue
                composed_slots[slot] = n

        # Garments map merge. Same under-vs-over rule: under-layers
        # only ship garment ids for slots they actively fill (state 1
        # in their own clothing_slots), so an under-bikini's `top`
        # garment field (state 3 = off) doesn't blow away the
        # primary's top garment.
        acc_garments = acc_props.get("garments") or {}
        if isinstance(acc_garments, dict):
            for slot, garment in acc_garments.items():
                if not isinstance(garment, str) or not garment:
                    continue
                if is_under:
                    own_state = acc_slots.get(slot)
                    if not isinstance(own_state, (int, float)) or int(own_state) != 1:
                        continue
                composed_garments[slot] = garment

    composed_props["coverage"] = composed_coverage
    composed_props["clothing_slots"] = composed_slots
    composed_props["garments"] = composed_garments
    return composed


def _accessories_line(
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]],
) -> str:
    """Render a comma-joined concise summary of a character's
    accessories list, suitable for a ``Accessories: ...`` line in
    the character card. Templates each accessory's concise text with
    {color} / {material} substitution so the same shared accessory
    file reads differently per character. Returns "" when no
    accessories are set."""
    char_props = char.get("properties") or {}
    accessory_ids = char_props.get("accessories") or []
    if not isinstance(accessory_ids, list) or not accessory_ids:
        return ""
    parts: list[str] = []
    for acc_id in accessory_ids:
        if not isinstance(acc_id, str):
            continue
        acc = _resolved_outfit(acc_id, entities)
        if not acc:
            # Fall back to the global template catalog when the
            # accessory hasn't been instanced into the conversation.
            global_tmpl = ent_mod.get(acc_id)
            if isinstance(global_tmpl, dict) and global_tmpl.get("type") == "outfit":
                acc = global_tmpl
        if not acc:
            continue
        acc_props = acc.get("properties") or {}
        text = (
            acc_props.get("concise_description")
            or acc_props.get("intact_description")
            or acc.get("description")
            or acc.get("name")
            or acc_id
        )
        text = _apply_outfit_template(text, acc, char)
        if text:
            parts.append(text)
    return "; ".join(parts)


def _body_marks_line(char: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Render persistent body marks (tattoos, piercings) as a single
    semicolon-joined line for the character card. Marks live under
    ``properties.body_marks: {<part>: "free text"}`` and stay across
    outfit changes — they describe the character, not the outfit.
    Returns "" when no marks are set."""
    char_props = char.get("properties") or {}
    marks = char_props.get("body_marks") or {}
    if not isinstance(marks, dict) or not marks:
        return ""
    out: list[str] = []
    for part, text in marks.items():
        if not isinstance(text, str) or not text.strip():
            continue
        rendered = apply_macros(text, ctx).strip()
        if rendered:
            label = part.replace("_", " ").capitalize() if isinstance(part, str) else "Mark"
            out.append(f"{label}: {rendered}")
    return "; ".join(out)


def _body_mark_for_part(char: dict[str, Any], part: str) -> str:
    """Return the persistent body mark for a single part, or "".
    Used by ``_body_description`` to append the mark to the per-part
    description so the model sees the tattoo / piercing on EVERY
    render of that part — covered or uncovered."""
    char_props = char.get("properties") or {}
    marks = char_props.get("body_marks") or {}
    if not isinstance(marks, dict):
        return ""
    val = marks.get(part)
    if not isinstance(val, str):
        return ""
    return val.strip()


def _resolved_outfit(
    outfit_id: str,
    entities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Walk the properties.extends chain (child → base) and merge.

    Top-level fields and `properties` are shallow-merged with the child
    winning. `properties.coverage` (a per-part dict) is one-level merged so
    a child outfit can override individual parts without redeclaring all of
    them. Cycles are stopped at the first repeat.
    """
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    cur: str | None = outfit_id
    while cur and cur not in seen and cur in entities:
        seen.add(cur)
        ent = entities[cur]
        chain.append(ent)
        nxt = (ent.get("properties") or {}).get("extends")
        cur = nxt if isinstance(nxt, str) else None
    if not chain:
        return {}
    if len(chain) == 1:
        return chain[0]
    merged: dict[str, Any] = {}
    # Walk base → child so the child's fields land last.
    for ent in reversed(chain):
        for k, v in ent.items():
            if k == "properties" and isinstance(v, dict) and isinstance(merged.get("properties"), dict):
                base_props = merged["properties"]
                new_props = {**base_props, **v}
                # Coverage: per-part merge so child can override one part only.
                if isinstance(base_props.get("coverage"), dict) and isinstance(v.get("coverage"), dict):
                    new_props["coverage"] = {**base_props["coverage"], **v["coverage"]}
                merged["properties"] = new_props
            else:
                merged[k] = v
    return merged


def _entity_one_liner(ent: dict[str, Any], ctx: dict[str, Any]) -> str:
    name = ent.get("name") or ent.get("id")
    desc = apply_macros((ent.get("description") or "").strip(), ctx)
    return f"{name}: {desc}" if desc else str(name)


def _object_block(
    obj: dict[str, Any],
    ctx: dict[str, Any],
    entities: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Render an object entity for the system prompt with its mechanical
    fields surfaced — effect, limitations, slot. The plain
    ``_entity_one_liner`` only emits name + description, which loses the
    most important gameplay content for cursed/magical items (the
    effect text the narrator and characters need to act on).

    When the object carries ``properties.equipped_to`` and the named
    character is in ``entities``, prepend the relationship to the head
    so the model reads the object as worn/carried by that character
    rather than free in the room."""
    name = obj.get("name") or obj.get("id")
    desc = apply_macros((obj.get("description") or "").strip(), ctx)
    props = obj.get("properties") or {}
    slot = (props.get("slot") or "").strip()
    effect = apply_macros((props.get("effect") or "").strip(), ctx)
    limitations = apply_macros((props.get("limitations") or "").strip(), ctx)
    equipped_to = props.get("equipped_to")
    equipped_prefix = ""
    if isinstance(equipped_to, str) and equipped_to:
        owner_name = equipped_to
        if entities and equipped_to in entities:
            owner_name = entities[equipped_to].get("name") or equipped_to
        equipped_prefix = f"(equipped to {owner_name}) "
    head = f"{equipped_prefix}{name}: {desc}" if desc else f"{equipped_prefix}{name}"
    if slot and slot != "none":
        head += f" (slot: {slot})"
    parts = [head]
    if effect:
        parts.append(f"  Effect: {effect}")
    if limitations:
        parts.append(f"  Limitations: {limitations}")
    return "\n".join(parts)


def _equipped_text(
    equipped: Any,
    entities: dict[str, dict[str, Any]],
    ctx: dict[str, Any],
) -> str:
    """Render the focal character's properties.equipped list as a short
    one-line-per-item block. Each line carries name + slot (if any) +
    effect (if any) so the model reads what's on the character right
    along with the body description.

    Equipped objects are deliberately NOT in the branch's effective
    cast (they're scoped to the character, not free items in scene),
    so the resolver falls back to the global entity catalog when the
    id isn't in the branch's instance entities. Lets the object's
    name + effect text surface inside the character card without
    making the object a cast member."""
    if not isinstance(equipped, list):
        return ""
    from . import entities as _ent_mod
    lines: list[str] = []
    for oid in equipped:
        if not isinstance(oid, str):
            continue
        ent = entities.get(oid)
        if not ent or ent.get("type") != "object":
            # Branch cast doesn't have it — try the global library.
            try:
                ent = _ent_mod.get(oid)
            except Exception:
                ent = None
        if not ent or ent.get("type") != "object":
            lines.append(f"- {oid} (equipped, definition missing)")
            continue
        name = ent.get("name") or oid
        desc = apply_macros((ent.get("description") or "").strip(), ctx)
        props = ent.get("properties") or {}
        slot = (props.get("slot") or "").strip()
        effect = apply_macros((props.get("effect") or "").strip(), ctx)
        head = f"- {name}"
        if slot and slot != "none":
            head += f" ({slot})"
        if desc:
            head += f": {desc}"
        if effect:
            head += f" — Effect: {effect}"
        lines.append(head)
    return "\n".join(lines)


def _cast_summary(
    entities: dict[str, dict[str, Any]],
    ctx: dict[str, Any],
) -> str:
    # The `user` entity is described separately in the `[The user]`
    # block so it doesn't belong in the cast list (which is "the other
    # characters in this scene"). Filter it out here.
    #
    # `entities` is already scoped to this branch's effective cast by
    # `branch_filter` (see assemble_prompt / prompt.context), which
    # applies the scene_staging_picks whitelist AND replays
    # cast_add/cast_remove. So a Nadia-only stage already arrives here
    # without Iris/Milo/Dex, and — crucially — a character
    # RE-ADDED after staging (or minted by the narrator) is present in
    # `entities` and therefore correctly surfaces in this block. We do
    # NOT re-filter by the write-once staging picks: that list is never
    # reconciled with later cast_add edits, so filtering by it would
    # hide re-added / newly-introduced characters from the narrator even
    # though they're on the branch.
    chars = [
        e for e in entities.values()
        if e.get("type") == "character" and e.get("id") != "user"
    ]
    if not chars:
        return ""
    return "\n".join(_cast_line(c, entities, ctx) for c in chars)


def _absent_cast_note(
    conversation: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    ctx: dict[str, Any],
) -> str:
    """A note naming scenario-pool characters who are NOT on this branch.

    Motivation: a character's own authored bio can reference clubmates by
    name (e.g. Iris's card mentions "Nadia's chaos … Milo's
    prickle"). When those regulars are staged out, the cast machinery
    correctly drops them from the prompt — but the model can still pull
    them into the scene off the bio prose. This note tells the model
    explicitly who exists-but-is-absent so it stops reintroducing them.

    Set = the instance scenario's `characters` pool MINUS the characters
    on this branch (`entities` is already branch-filtered to the
    effective cast). Resolved to DISPLAY NAMES and de-duplicated against
    present names, so a b-side that shares its A-side's name (Iris /
    iris_bside) is never listed as "absent" while the A-side is present.
    Empty (no note) on non-staged branches where the whole pool is
    present. Applies to both the narrator and character prompts.
    """
    cid = conversation.get("id")
    scenario_id = conversation.get("scenario_id")
    if not cid or not scenario_id:
        return ""
    scen = ent_mod.load_instance_entity(cid, scenario_id) or {}
    pool = [c for c in (scen.get("characters") or []) if isinstance(c, str) and c]
    if not pool:
        return ""

    present_names: set[str] = set()
    for e in entities.values():
        if e.get("type") == "character":
            nm = (e.get("name") or e.get("id") or "").strip()
            if nm:
                present_names.add(nm)
    user_name = (ctx or {}).get("user_name")
    if isinstance(user_name, str) and user_name.strip():
        present_names.add(user_name.strip())

    from . import bside
    absent: list[str] = []
    seen: set[str] = set()
    for pid in pool:
        if pid == "user" or pid in entities:
            continue  # on this branch already
        inst = ent_mod.load_instance_entity(cid, pid) or ent_mod.get(pid) or {}
        # Skip b-sides whose A-side is present: a b-side is an alternate
        # version of a character already in the scene, not a genuinely
        # absent third party. Listing "Iris (B-side)" as absent while
        # Iris is present only confuses the model.
        a_side = bside.a_side_id(inst) or bside.a_side_id(pid)
        if a_side and a_side in entities:
            continue
        nm = (inst.get("name") or pid).strip()
        if not nm or nm in present_names or nm in seen:
            continue
        seen.add(nm)
        absent.append(nm)
    if not absent:
        return ""
    names = ", ".join(absent)
    return (
        f"Not in this scene: {names}. These characters exist in this world "
        "but are NOT present right now — do not have them appear, speak, act, "
        "or be addressed as if present unless the story explicitly brings them "
        "into the scene."
    )


def _cast_line(
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    ctx: dict[str, Any],
) -> str:
    """One narrator-facing cast entry: id + description + the outfits
    the narrator can target via ``[outfit <id> -> <outfit_id>]``.

    Each available outfit is rendered with a short description so the
    narrator can disambiguate ID-similar outfits — without it the model
    can't tell ``dex_thin_shirt`` ("thin shirt riding up") from
    ``dex_shirt_up`` ("shirt rolled up to the collarbone, undershirt
    showing") when the user asks for "shirts up". Surfacing the available
    list also stops the model reaching for fenced ```edits``` JSON
    patches with invented schemas (e.g. ``data.clothing.shirt_status =
    "up"``) when no outfit id is in scope.
    """
    char_id = char.get("id") or char.get("name") or ""
    name = char.get("name") or char_id
    desc = apply_macros((char.get("description") or "").strip(), ctx)
    head = f"- {name} (id: `{char_id}`): {desc}" if desc else f"- {name} (id: `{char_id}`)"

    props = char.get("properties") or {}
    current = props.get("current_outfit")
    owned_ids = sorted(
        e for e in entities
        if entities[e].get("type") == "outfit"
        and (entities[e].get("properties") or {}).get("owner") == char_id
    )

    # `properties.notes` is the open destination for narrator-add edits.
    # Convention: each key is a short snake_case label (e.g. `mother`,
    # `relationship`, `injury`, `mood`), value is a short descriptive
    # string. Surface them as bullets so the model on the next turn
    # sees the facts the narrator wrote on prior turns. Skip non-string
    # values defensively in case a directive wrote a nested dict.
    raw_notes = props.get("notes") if isinstance(props.get("notes"), dict) else None
    notes_items: list[tuple[str, str]] = []
    if raw_notes:
        for k, v in raw_notes.items():
            if not isinstance(k, str) or not k:
                continue
            if isinstance(v, str):
                text = apply_macros(v.strip(), ctx).strip()
            else:
                text = str(v)
            if not text:
                continue
            if len(text) > 180:
                text = text[:177].rstrip() + "…"
            notes_items.append((k, text))

    if not owned_ids and not current and not notes_items:
        return head

    lines: list[str] = []
    if current:
        lines.append(f"  current_outfit: `{current}`")
    if owned_ids:
        lines.append("  available outfits:")
        for oid in owned_ids:
            blurb = _outfit_blurb(entities[oid], ctx)
            lines.append(f"    - `{oid}`: {blurb}" if blurb else f"    - `{oid}`")
    if notes_items:
        lines.append("  notes:")
        for k, v in notes_items:
            lines.append(f"    - {k}: {v}")
    return head + "\n" + "\n".join(lines)


def _outfit_blurb(outfit: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Short description for the cast list. Prefer the outfit's
    ``concise_description``, fall back to its name + condition tag, then
    its top-level description (truncated). Macros applied so {{user}} /
    {{char}} substitutions don't leak through verbatim."""
    props = outfit.get("properties") or {}
    text = (
        props.get("concise_description")
        or outfit.get("example_text")
        or outfit.get("description")
        or outfit.get("name")
        or ""
    ).strip()
    text = apply_macros(text, ctx).strip()
    if len(text) > 180:
        text = text[:177].rstrip() + "…"
    return text


def _world_summary(entities: dict[str, dict[str, Any]], ctx: dict[str, Any]) -> str:
    locations = [e for e in entities.values() if e.get("type") == "location"]
    rooms = [e for e in entities.values() if e.get("type") == "room"]
    objects = [e for e in entities.values() if e.get("type") == "object"]
    parts: list[str] = []
    if locations:
        parts.append("Locations:\n" + "\n".join(f"- {_entity_one_liner(l, ctx)}" for l in locations))
    if rooms:
        parts.append("Rooms:\n" + "\n".join(f"- {_entity_one_liner(r, ctx)}" for r in rooms))
    if objects:
        parts.append("Objects:\n" + "\n".join(_object_block(o, ctx, entities) for o in objects))
    return "\n\n".join(parts)


def _surroundings_text(
    entities: dict[str, dict[str, Any]],
    presence: dict[str, Any],
    ctx: dict[str, Any],
) -> str:
    parts: list[str] = []
    location_id = presence.get("location")
    room_id = presence.get("room")
    if location_id and location_id in entities:
        loc = entities[location_id]
        parts.append(f"You are in: {loc.get('name', location_id)} — {apply_macros(loc.get('description', ''), ctx).strip()}")
    if room_id and room_id in entities:
        room = entities[room_id]
        parts.append(f"Room: {room.get('name', room_id)} — {apply_macros(room.get('description', ''), ctx).strip()}")
        # Sensory anchors — surface the room's `lighting`, `ambient_sounds`,
        # and `scent` properties as a structured Senses line if any are set.
        # Authors have been storing these on rooms for months but the
        # assembler only used to render whatever was duplicated into the
        # free-form description. Pulling them out gives the model
        # olfactory / auditory / lighting anchors it would otherwise miss.
        room_props = room.get("properties") or {}
        sense_bits: list[str] = []
        for key, label in (("lighting", "Light"), ("ambient_sounds", "Sound"), ("scent", "Scent")):
            val = room_props.get(key)
            if isinstance(val, str) and val.strip():
                sense_bits.append(f"{label}: {apply_macros(val.strip(), ctx)}")
        if sense_bits:
            parts.append("Senses:\n" + "\n".join(f"- {b}" for b in sense_bits))
        children = room.get("children") or []
        objs = [entities[c] for c in children if c in entities and entities[c].get("type") == "object"]
        if objs:
            parts.append("Objects here:\n" + "\n".join(_object_block(o, ctx, entities) for o in objs))
    return "\n\n".join(parts)


def _others_present_text(
    focal_id: str,
    entities: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
    ctx: dict[str, Any],
) -> str:
    """Render visible cards for other characters in the same room as the
    focal character. Each card includes the other's name, current outfit,
    and the body description filtered through their outfit's coverage so
    covered parts surface as garment details and only uncovered / sheer
    parts surface bare. Internal state (personality, relationships,
    body_hair, scent, background) is omitted — those don't belong in
    another character's prompt."""
    focal_presence = _latest_presence_for(history, focal_id)
    if not focal_presence:
        return ""
    cards: list[str] = []
    for cid, ent in entities.items():
        if cid == focal_id or ent.get("type") != "character":
            continue
        # The user is rendered separately in the `[The user]` block so
        # they don't double-up here as just another cast member.
        if cid == "user":
            continue
        their_presence = _latest_presence_for(history, cid)
        # Intentionally fail-CLOSED (unlike the user-input gate in
        # _history_visible_to): an unplaced co-character must NOT appear
        # in everyone's "others present" block. Over-inclusion here
        # would pull not-actually-present cast into the prompt; under-
        # inclusion just omits a character the scene can reintroduce.
        # Co-characters get presence from the scenario seed, so the
        # missing case is rare and excluding is the safe default.
        if not _same_scene(focal_presence, their_presence):
            continue
        their_outfit = (their_presence or {}).get("outfit")
        cards.append(_other_character_card(ent, entities, ctx, current_outfit_override=their_outfit))
    return "\n\n".join(c for c in cards if c)


def _other_character_card(
    char: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    ctx: dict[str, Any],
    current_outfit_override: str | None = None,
) -> str:
    name = char.get("name") or char.get("id")
    out = [str(name)]
    props = char.get("properties") or {}

    outfit = None
    # Same presence-outfit-wins logic as the focal character card.
    outfit_id = current_outfit_override or props.get("current_outfit")
    if outfit_id and outfit_id in entities:
        outfit = _resolved_outfit(outfit_id, entities)
        outfit_props = outfit.get("properties") or {}
        outfit_text = (
            outfit_props.get("intact_description")
            or outfit_props.get("concise_description")
            or outfit.get("description")
            or outfit.get("name")
        )
        if outfit_text:
            out.append(f"Wearing: {apply_macros(outfit_text, ctx)}")

    body_text = _body_description(char, outfit, ctx)
    if body_text:
        out.append("Visible appearance:\n" + body_text)
    else:
        # Non-explicit (SFW) format fallback — these characters carry a
        # prose `appearance` map instead of a per-part body description.
        appearance = props.get("appearance")
        if isinstance(appearance, dict) and appearance:
            appear_lines = []
            for k, v in appearance.items():
                if isinstance(v, str) and v.strip():
                    label = str(k).replace("_", " ").capitalize()
                    appear_lines.append(f"- {label}: {apply_macros(v.strip(), ctx)}")
            if appear_lines:
                out.append("Visible appearance:\n" + "\n".join(appear_lines))

    extra_lines = _extra_state_lines(char, ctx)
    if extra_lines:
        out.append("State:\n" + "\n".join(extra_lines))

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Author's note + post-history + stop strings
# ---------------------------------------------------------------------------


def _inject_author_note(
    messages: list[dict[str, str]],
    settings: dict[str, Any],
    ctx: dict[str, Any],
    pieces: list[dict[str, str]],
    focal_id: str | None = None,
) -> list[dict[str, str]]:
    # Cast-wide note, plus the focal character's own per-character note
    # (settings.author_note_per_character[<id>]) appended below it. Both
    # are edited live in the side panel; per-character only injects on
    # that character's own turn.
    note = (settings.get("author_note") or "").strip()
    per_note = ""
    per = settings.get("author_note_per_character")
    if focal_id and isinstance(per, dict):
        pn = per.get(focal_id)
        if isinstance(pn, str) and pn.strip():
            per_note = pn.strip()
    if not note and not per_note:
        return messages
    combined = apply_macros("\n\n".join(t for t in (note, per_note) if t), ctx)
    depth = max(0, int(settings.get("author_note_depth") or 1))
    pos = max(0, len(messages) - depth)
    pieces.append({"label": f"Author's note (depth {depth})", "content": combined})
    new = list(messages)
    new.insert(pos, {"role": "system", "content": f"[Author's Note]\n{combined}"})
    return new


def _append_post_history(
    messages: list[dict[str, str]],
    settings: dict[str, Any],
    ctx: dict[str, Any],
    pieces: list[dict[str, str]],
) -> list[dict[str, str]]:
    post = (settings.get("post_history_instructions") or "").strip()
    if not post:
        return messages
    post = apply_macros(post, ctx)
    pieces.append({"label": "Post-history instructions", "content": post})
    new = list(messages)
    new.append({"role": "system", "content": f"[Instructions]\n{post}"})
    return new


def _append_same_speaker_continue(
    messages: list[dict[str, str]],
    history: list[dict[str, Any]],
    character_id: str,
    character_name: str,
    pieces: list[dict[str, str]],
) -> list[dict[str, str]]:
    """If the most recent visible history message is already by the focal
    character, append a system instruction telling them to continue the
    scene rather than emit an empty turn.

    Without this, a Generate ↻ click (or an Auto Play tick) for the
    same character as the previous message lands the model in a prompt
    that ends with its own complete turn — and most models respond
    with EOS immediately. The empty completion fails the non-empty
    guard in the stream route and the client sees a streaming bubble
    that's never given a final message, which it then removes.
    """
    if not history:
        return messages
    last = history[-1] or {}
    if last.get("speaker_id") != character_id:
        return messages
    if last.get("persona") == "user":
        return messages
    note = (
        f"[Continue the scene]\n"
        f"The most recent turn above is yours. Write a fresh new beat "
        f"as {character_name} — a follow-up action, a reaction, an "
        f"escalation, an aside, a shift in focus. Do not repeat or "
        f"summarize what you just said; advance the moment."
    )
    pieces.append({"label": "Continue the scene", "content": note})
    new = list(messages)
    new.append({"role": "system", "content": note})
    return new


# ---------------------------------------------------------------------------
# World Info / Lorebook
# ---------------------------------------------------------------------------

LORE_RECENT_WINDOW = 5  # number of recent messages scanned for triggers


def _activated_lore(
    entities: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Return lore entries grouped by injection position.

    An entry activates when any of its triggers appears (case-insensitive)
    in the last LORE_RECENT_WINDOW messages, OR when properties.always_active
    is true.
    """
    lore = [e for e in entities.values() if e.get("type") == "lore"]
    if not lore:
        return {}
    recent_text = "\n".join(
        (m.get("content") or "") for m in history[-LORE_RECENT_WINDOW:]
    ).lower()
    grouped: dict[str, list[dict[str, Any]]] = {
        "before_char": [], "after_char": [], "at_depth": [],
    }
    for entry in lore:
        props = entry.get("properties") or {}
        always = bool(props.get("always_active"))
        triggers = [t for t in (props.get("triggers") or []) if isinstance(t, str)]
        if not always:
            if not any(t.lower() in recent_text for t in triggers if t.strip()):
                continue
        position = (props.get("position") or "after_char").lower()
        if position not in grouped:
            position = "after_char"
        grouped[position].append(entry)
    return grouped


def _format_lore(entries: list[dict[str, Any]], ctx: dict[str, Any]) -> str:
    parts: list[str] = []
    for e in entries:
        name = e.get("name") or e.get("id")
        body = (e.get("description") or "").strip()
        if not body:
            continue
        parts.append(f"- {name}: {apply_macros(body, ctx)}")
    return "\n".join(parts)


def _inject_lore_at_depth(
    messages: list[dict[str, str]],
    entries: list[dict[str, Any]],
    ctx: dict[str, Any],
) -> list[dict[str, str]]:
    """Insert at_depth lore entries as system messages at their per-entry depth."""
    if not entries:
        return messages
    out = list(messages)
    # Insert deepest first so positions stay valid.
    by_depth = sorted(
        entries,
        key=lambda e: int((e.get("properties") or {}).get("depth") or 0),
        reverse=True,
    )
    for e in by_depth:
        depth = max(0, int((e.get("properties") or {}).get("depth") or 0))
        pos = max(0, len(out) - depth)
        body = apply_macros(e.get("description") or "", ctx).strip()
        if not body:
            continue
        name = e.get("name") or e.get("id")
        out.insert(pos, {"role": "system", "content": f"[Lore — {name}]\n{body}"})
    return out


# ---------------------------------------------------------------------------
# Token-budgeted truncation
# ---------------------------------------------------------------------------


def _approx_tokens(s: str) -> int:
    # Cheap estimate: ~4 chars/token. Good enough for budgeting.
    return (len(s) + 3) // 4


def _truncate_history(
    messages: list[dict[str, str]],
    system_text: str,
    settings: dict[str, Any],
    cfg_default_options: dict[str, Any],
) -> tuple[list[dict[str, str]], int, list[dict[str, str]]]:
    """Drop oldest droppable user/assistant turns until the prompt fits.

    Pinned (never dropped):
      - Any system-role message (primer brackets, author's note, lore at_depth,
        post-history, summary).
      - The first 8 messages (the example primer block + opener).
      - The last 4 messages (recent turns).
    Returns (truncated_messages, dropped_count, dropped_messages).
    """
    sampling = (settings.get("sampling") or {})
    num_ctx = int(
        sampling.get("num_ctx")
        or (cfg_default_options.get("num_ctx") if isinstance(cfg_default_options, dict) else 0)
        or 8192
    )
    num_predict = int(
        sampling.get("num_predict")
        or (cfg_default_options.get("num_predict") if isinstance(cfg_default_options, dict) else 0)
        or 512
    )
    if num_predict < 0:
        num_predict = 512
    reserve = max(256, num_predict)

    # The user-visible "Context limit (tokens)" field clamps num_ctx.
    # Setting it lower than num_ctx means the truncator targets that
    # tighter budget regardless of what Ollama is willing to take.
    user_limit = settings.get("context_limit_tokens")
    if isinstance(user_limit, (int, float)) and user_limit > 0:
        num_ctx = min(num_ctx, int(user_limit))

    budget = max(0, num_ctx - reserve - _approx_tokens(system_text))
    used = sum(_approx_tokens(m["content"]) for m in messages)
    if used <= budget:
        return messages, 0, []

    n = len(messages)
    head_end = min(8, n)
    tail_start = max(head_end, n - 4)
    head = messages[:head_end]
    middle = messages[head_end:tail_start]
    tail = messages[tail_start:]

    remaining = budget - sum(_approx_tokens(m["content"]) for m in head + tail)
    kept_middle: list[dict[str, str]] = []
    dropped_msgs: list[dict[str, str]] = []
    for m in reversed(middle):
        if m["role"] == "system":
            kept_middle.insert(0, m)
            continue
        cost = _approx_tokens(m["content"])
        if cost <= remaining:
            kept_middle.insert(0, m)
            remaining -= cost
        else:
            dropped_msgs.insert(0, m)
    if dropped_msgs:
        marker = {
            "role": "system",
            "content": f"[…{len(dropped_msgs)} earlier message{'s' if len(dropped_msgs) != 1 else ''} elided to fit context…]",
        }
        kept_middle.insert(0, marker)
    # Only REAL history is summarizable. The messages array opens with
    # the synthetic example-dialogue primer, so the oldest "middle"
    # turns the loop above elides first are usually PRIMER PAIRS — and
    # the route feeds dropped messages to the background summarizer.
    # Without this filter the summarizer turned a character's example
    # dialogue into fabricated conversation memory (live repro:
    # 2-real-turn conversation carrying "[Summary so far] …the two
    # shared a long conversation in her dorm…" — the primer's
    # relationship arc, with anchor_ids=[] because primer messages have no
    # ids). Real history messages carry __msg_id from _history_msg;
    # synthetic ones (primer, narrator beats, injected notes) don't.
    summarizable = [m for m in dropped_msgs if m.get("__msg_id")]
    return head + kept_middle + tail, len(dropped_msgs), summarizable


def _user_stop_strings(user_name: str) -> list[str]:
    """The user-handback stops only: `\\n<user_name>:` and `\\nUser:`.

    Split out from :func:`_stop_strings` so the multi-response path can
    keep JUST these (dropping every per-character label) while a normal
    single-character turn keeps the full set. See `stop_user_only` in the
    character-assembly return.
    """
    out = {f"\n{user_name}:", "\nUser:"}
    return [s for s in out if s and s.strip() != ":"]


def _stop_strings(
    entities: dict[str, dict[str, Any]],
    user_name: str,
    exclude_id: str | None = None,
) -> list[str]:
    """Speaker-handoff stops, each anchored to a line start (`\\n<Name>:`).

    Anchoring is load-bearing: Ollama matches stops as literal substrings,
    so a bare `"<Name>:"` also fires on an INCIDENTAL in-prose mention
    ("...she glanced at Nadia:") or a roster echo ("1. Iris:"),
    truncating the message mid-sentence. A genuine handoff to another
    speaker always begins a new line, so `"\\n<Name>:"` catches the real
    case (including `"\\n\\n<Name>:"`) without the false positives.
    """
    out: set[str] = set(_user_stop_strings(user_name))
    for c in entities.values():
        if c.get("type") != "character":
            continue
        if c.get("id") == exclude_id:
            continue
        n = c.get("name")
        if n:
            out.add(f"\n{n}:")
    return [s for s in out if s and s.strip() != ":"]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def _history_msg(
    msg: dict[str, Any],
    entities: dict[str, dict[str, Any]],
    ctx: dict[str, Any] | None = None,
    focal_id: str | None = None,
    number_multi_groups: dict[str, int] | None = None,
) -> dict[str, str]:
    """Render one history message as a chat-completion msg dict.

    Role assignment respects the focal speaker about to generate.

    For character assembly (focal_id == character id): only the focal's
    own past turns get role="assistant"; every other message is
    role="user" (input the focal reacts to):

      - persona == "user"                  → "user"
      - persona == "narrator"              → "user" (scene-setting input)
      - non-focal character message        → "user" (someone else spoke)
      - focal character's own past message → "assistant"

    For narrator assembly (focal_id == "narrator"): prior narrator beats
    are role="assistant" (so the model can continue narrator-after-
    narrator chains); everything else is role="user".

    Default (focal_id is None) preserves the old persona!=user→assistant
    behavior — kept for any caller that hasn't been updated.

    Pre-this-fix, every non-user message was role="assistant" regardless
    of who spoke. That broke chat-instruct alternation when the prompt
    ended with a non-focal assistant turn (user-typed-as-NPC, multi-NPC
    AI scenes, narrator beats): model EOSed immediately because the
    prompt looked "complete" from its perspective, the empty completion
    fell through `persist_partial`'s non-empty guard, and the client
    removed the placeholder bubble — looking like "no response at all".
    A narrow narrator-only re-roling existed at the caller site
    (`_assemble_character` post-loop) as a partial fix; the proper
    fix lives here, owned by the message renderer.
    """
    persona = msg["persona"]
    speaker = msg.get("speaker_id")
    if persona == "user":
        role = "user"
    elif focal_id == "narrator":
        role = "assistant" if persona == "narrator" else "user"
    elif focal_id is None:
        # Back-compat path for unmigrated callers — old persona!=user
        # rule, which treats any non-user message as assistant.
        role = "user" if persona == "user" else "assistant"
    elif speaker == focal_id or persona == focal_id:
        role = "assistant"
    else:
        # Narrator beat, or another character spoke (AI-generated turn,
        # or the user voicing them). Either way, this is input the
        # focal reacts to, not the focal's own output.
        role = "user"
    label = ""
    if persona == "narrator":
        label = "Narrator: "
    elif speaker and speaker in entities:
        label = f"{entities[speaker].get('name', speaker)}: "
    elif persona == "user":
        label = "User: "
    content = (msg.get("content") or "").strip()
    # Multi prompts: prior multi-group messages re-render with the
    # numbered-label format the joint directive demands, so history
    # teaches the format instead of contradicting it with plain
    # "Name: body" examples. Numbers come from the CURRENT roster
    # (lead = 1, partners in order) — NOT the message's own group
    # ordinal: leads rotate between turns, so a character numbered by
    # her old group ("1. Risa…" when she led) contradicts today's
    # roster ("3. Risa…") and the model spirals debating who's who
    # (replay run 3: "the prompt had Rika/Risa mix-ups" → 30-line
    # planning loop). Speakers no longer on the roster keep their
    # plain "Name:" label. Bodies also get the meta scrub at render
    # time — an already-persisted planning leak would otherwise keep
    # being shown back to the model as a valid character turn.
    if number_multi_groups:
        grp = (msg.get("metadata") or {}).get("multi_response")
        if isinstance(grp, dict) and label:
            speaker_name = (
                entities.get(speaker, {}).get("name") if speaker else None
            )
            roster_no = (
                number_multi_groups.get(speaker_name.lower())
                if isinstance(number_multi_groups, dict) and speaker_name
                else None
            )
            if roster_no:
                label = f"{roster_no}. {label}"
            from .multi_response import strip_meta_commentary
            content = strip_meta_commentary(content)
    if ctx:
        # Per-message ctx: char_name follows the actual speaker if known, so
        # macros render that turn's character correctly.
        per_ctx = dict(ctx)
        if speaker and speaker in entities and entities[speaker].get("name"):
            per_ctx["char_name"] = entities[speaker]["name"]
        content = apply_macros(content, per_ctx)
    # Carry the original message id through so the truncator + summarizer
    # can anchor settings.summary to specific ids and detect when the user
    # branches/deletes away from the summarized region.
    return {"role": role, "content": f"{label}{content}".rstrip(), "__msg_id": msg.get("id")}


def _history_preview(messages: list[dict[str, str]]) -> str:
    return "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)


def _style_discipline_block() -> str:
    """Read the global banned-phrases list from app config and turn it into
    a [Style discipline] system block so the model has soft guidance to
    avoid the phrases. Empty list = no block."""
    try:
        from flask import current_app
        cfg = current_app.config
    except Exception:
        return ""
    sd = ((cfg.get("defaults") or {}).get("style_discipline") or {})
    phrases = sd.get("banned_phrases") or []
    phrases = [p.strip() for p in phrases if isinstance(p, str) and p.strip()]
    if not phrases:
        return ""
    listed = ", ".join(f'"{p}"' for p in phrases[:30])
    return (
        "[Style discipline]\n"
        "Never use these phrases verbatim, and avoid close paraphrases. If "
        "your draft would contain one, rewrite it before responding. "
        f"Forbidden: {listed}."
    )


def banned_phrase_hits(text: str) -> list[str]:
    """Case-insensitive substring scan against the configured banned phrases.
    Returns the list of phrases that appear in `text`. Empty list when
    nothing's configured or nothing matched."""
    try:
        from flask import current_app
        cfg = current_app.config
    except Exception:
        return []
    sd = ((cfg.get("defaults") or {}).get("style_discipline") or {})
    phrases = [p.strip() for p in (sd.get("banned_phrases") or []) if isinstance(p, str) and p.strip()]
    if not phrases or not text:
        return []
    lower = text.lower()
    return [p for p in phrases if p.lower() in lower]


def _summary_anchor_in_path(anchor_ids: list[str], history: list[dict[str, Any]]) -> bool:
    """Return True iff EVERY anchor message id is present in the active
    path. Strict per-branch semantics: any anchor that's off the path
    means this summary fragment describes a sibling branch's content
    and must not bleed into the active prompt. (The previous behaviour
    used any(), which would mark a fragment 'fresh' whenever a single
    shared ancestor anchor matched — and that's exactly how sibling-
    branch summaries leaked across to the current path.)"""
    if not anchor_ids:
        return True
    path_ids = {m.get("id") for m in history if m.get("id")}
    return all(aid in path_ids for aid in anchor_ids)


def _resolve_summary_text(
    settings: dict[str, Any],
    history: list[dict[str, Any]],
    ctx: dict[str, Any],
    focal_id: str | None = None,
) -> tuple[str, str]:
    """Return (active_text, stale_text) for the [Summary so far] block.

    PER-CHARACTER: a character focal reads only its OWN fragments
    (`settings.summary_fragments_by_focal[focal]`) — a recap of what THAT
    character witnessed, never the omniscient global summary (which would
    re-introduce the locational leak the rest of the stack closes). The
    narrator (focal_id None / "narrator") reads the global
    `settings.summary_fragments` — it is authorial and sees everything —
    plus the legacy single-string form for un-resummarized conversations.

    Each fragment is filtered by its anchor_ids (every anchor on the active
    path → fresh; else stale, surfaced only in the dev panel), preserving
    per-branch isolation.
    """
    def _filter(fragments: Any) -> tuple[str, str]:
        active_parts: list[str] = []
        stale_parts: list[str] = []
        path_ids = {m.get("id") for m in history if m.get("id")}
        for frag in fragments or []:
            if not isinstance(frag, dict):
                continue
            text = (frag.get("text") or "").strip()
            if not text:
                continue
            anchors = [a for a in (frag.get("anchor_ids") or []) if isinstance(a, str)]
            (active_parts if (not anchors or all(a in path_ids for a in anchors)) else stale_parts).append(text)
        return (apply_macros("\n\n".join(active_parts), ctx).strip(),
                apply_macros("\n\n".join(stale_parts), ctx).strip())

    if focal_id and focal_id != "narrator":
        per_focal = (settings.get("summary_fragments_by_focal") or {}).get(focal_id)
        if isinstance(per_focal, list) and per_focal:
            return _filter(per_focal)
        return "", ""  # no witnessed summary yet — never fall back to the omniscient one

    fragments = settings.get("summary_fragments")
    if isinstance(fragments, list) and fragments:
        return _filter(fragments)

    legacy_text = apply_macros((settings.get("summary") or "").strip(), ctx)
    legacy_anchors = settings.get("summary_anchor_ids") or []
    if not legacy_text:
        return "", ""
    if legacy_anchors and not _summary_anchor_in_path(legacy_anchors, history):
        return "", legacy_text
    return legacy_text, ""


def _user_speaking_as(
    history: list[dict[str, Any]],
    entities: dict[str, dict[str, Any]],
    *,
    exclude_id: str | None = None,
) -> str | None:
    """If the user's most recent non-narrator turn was them speaking as a
    character (persona = a character entity, not "user"), return that
    character's id. The responder prompt uses this to label the line as
    "the user is currently giving voice to <X>" so it doesn't mistake X's
    words for the user's. Returns None when the user is speaking as
    themselves, or when the active voice would just be the responder."""
    for msg in reversed(history):
        persona = msg.get("persona")
        if persona == "narrator":
            continue
        if persona == "user":
            return None
        if persona == exclude_id:
            return None
        if persona in entities and entities[persona].get("type") == "character":
            speaker_id = msg.get("speaker_id") or persona
            return speaker_id if speaker_id != exclude_id else None
        return None
    return None


def _latest_presence_for(history: list[dict[str, Any]], character_id: str) -> dict[str, Any]:
    for msg in reversed(history):
        snap = msg.get("presence_snapshot") or {}
        presence = snap.get("presence") or {}
        if character_id in presence:
            return presence[character_id]
    return {}


def _scene_gated_out(
    focal_loc: dict[str, Any] | None,
    other_loc: dict[str, Any] | None,
) -> bool:
    """Locational-memory gate — the single source of the FAIL-OPEN rule.

    True (hide) ONLY when BOTH placements are known and they are different
    scenes. A missing placement on either side returns False (show), so any
    branch that never emitted a `[move x -> room]` edit (legacy scenarios, the
    default scene_staging root, pre-gate conversations) never over-hides — the
    load-bearing guard that keeps a focal from losing every user/narrator line
    (which surfaced as format drift + hallucinated input). Used by both the
    history filter and the user-persona block so the rule lives in one place."""
    return bool(focal_loc and other_loc and not _same_scene(focal_loc, other_loc))


def _history_visible_to(
    history: list[dict[str, Any]],
    character_id: str,
) -> list[dict[str, Any]]:
    """Filter history to what ``character_id`` could have WITNESSED — messages
    from a scene they shared. Fail-open via `_scene_gated_out` (see there)."""
    visible: list[dict[str, Any]] = []
    for msg in history:
        presence = (msg.get("presence_snapshot") or {}).get("presence") or {}
        char_loc = presence.get(character_id)
        speaker_id = msg.get("speaker_id")
        if speaker_id and speaker_id != character_id:
            # another character spoke — heard only if co-located
            if _scene_gated_out(char_loc, presence.get(speaker_id)):
                continue
        elif not speaker_id and msg.get("persona") == "user":
            # user-authored line (speaker_id null): gate on the user's placement
            if _scene_gated_out(char_loc, presence.get("user")):
                continue
        elif not speaker_id and msg.get("persona") == "narrator":
            # scene-setting narration: the focal must at least be placed to be in it
            if char_loc is None:
                continue
        visible.append(msg)
    return visible


def _same_scene(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if not a or not b:
        return False
    # Rooms must match. Locations must match too WHEN both are known —
    # but a missing location on either side falls back to the room match
    # rather than failing closed. Room ids are globally unique (every
    # room is the child of exactly one location), so a shared room is an
    # unambiguous same-scene signal. This heals presence snapshots where
    # a bare `[move x -> room]` left location=None (e.g. an NPC the
    # narrator dropped into an existing room): without the fallback,
    # `None == 'trap_apartment'` is False and the NPC is gated out of a
    # co-located character's prompt even though they share the room.
    if a.get("room") != b.get("room"):
        return False
    la, lb = a.get("location"), b.get("location")
    return (la == lb) or (not la) or (not lb)
