"""Narrator-driven 'add element' to an active scene.

Sibling of ``narrator_edit``. Same return shape, same chat_stream
plumbing, same edit-grammar parser — what differs is the system
prompt + the available-data block. Where ``narrator_edit`` says
"do not invent ids", ``narrator_add`` lists off-cast character
templates and tells the model it MAY use them via ``[move <id> ->
<room>]`` to bring them into the scene.

Why a separate entry point: editing-an-existing-message and
introducing-a-new-element have opposing invariants. The edit prompt
must reject typo'd ids so a confused narrator doesn't materialize a
junk character mid-conversation; the add prompt must allow off-cast
ids so the user can say "Rosa walks in" and have it land. Trying
to merge them in one prompt produced inconsistent model behavior:
simple-action directives flowed, compound directives caused the
model to retreat to no-op restating-of-state. See the
test_narrator_add_rosa_mom report under tools/tools/ for the
empirical evidence.

This module ships ONLY the prompt + sync entry point. Full feature
support also needs:
  - ``narrator_apply._record_edit("move"/"patch", ...)`` extended
    to auto-instance off-cast character templates (option 2 from
    prior research).
  - ``personas._cast_line`` to render ``properties.notes`` so the
    relationship facts the model writes here actually surface to
    the next-turn prompt (option A).

Without those, this module emits the right edits and the right
prose; the disk-side instancing simply doesn't happen until you
ship the apply-side handlers.
"""
from __future__ import annotations

from typing import Any

from . import entities as ent
from .entities import load_instance_entities
from .narrator import extract_edits
from .narrator_edit import _build_world_summary, _speaker_label_for
from .ollama_client import chat_stream
from .personas import compose_wardrobe_extra


ADD_SYSTEM_TEMPLATE = """\
You are introducing a new element into an interactive roleplay scene. The user gave a directive describing what to add — a new character entering, a relationship fact, a status change, an object appearing. Your job is to encode the new state as edit directives AND rewrite the message body so the new element is introduced naturally.

Output format (exactly this — directives first, blank line, then the rewritten body):

[move <character_id> -> <room_id>]
[outfit <character_id> -> <outfit_id>]
[equip <character_id>.<slot> = <piece_id>]
[unequip <character_id>.<slot>]
[state <character_id> -> <state_id>]
[set <entity_id>.<dotted.path> = <value>]
[unset <entity_id>.<dotted.path>]

<the rewritten message body>

Rules:
1. Emit edit directives FIRST, each on its own line, before any prose.
2. Use ids from [Available data] below. Both the "Cast (in scene)" list AND the "Off-cast available" list are valid id sources. To bring an off-cast character into the scene, write `[move <off-cast-id> -> <room_id>]` — the system will instance them automatically. Do not invent ids for characters that ALREADY exist in either block.
2b. EITHER form works in directives — the cast-id (e.g. `guy_1`) OR the current display name (e.g. `Kenji`, case-insensitive). The system resolves names to ids at apply time. Exact-id match wins over name match. This means after `[set guy_1.name = "Kenji"]`, BOTH `[set guy_1.notes.x = y]` and `[set Kenji.notes.x = y]` route to the same entity. Pick whichever form is clearer in context — many narrators find names easier to remember mid-batch, and that's fine.
3. When the directive names a brand-new male character who is NOT in either list (e.g. "Iris has a brother named Jonah"), you MAY invent a new lowercase snake_case id for them (e.g. `jonah`). The system will materialise them from a generic male template; you should immediately emit `[set <new_id>.name = "Proper Name"]` so the body and future turns refer to them correctly. Add any further `[set <new_id>.properties.notes.<key> = <value>]` to record the relationship or role. Do not move a freshly-invented character into a room unless the directive explicitly has them entering the scene.
4. Per-character facts (relationships, mood, status, notes) go in `[set <character_id>.properties.notes.<key> = <value>]`. Use snake_case keys and short descriptive values. Symmetric relationships (e.g. "X is Y's mom" implies Y has X as mother) should write TWO mirror notes — one on each character — so both turns surface the fact.
5. When the directive establishes a fact involving an off-cast character (a relationship, role, or history) but does NOT bring them into the scene, still write `[set <off-cast-id>.properties.notes.<key> = <value>]` for the off-cast side of the relationship — the system auto-adds them to the cast as a known character (without placing them in any room) so future turns can reference or call on them. Skipping the off-cast `[set]` leaves them invisible to the rest of the scenario; always write both halves of a symmetric relationship.
6. When the directive moves characters to a place not already listed under [Available locations] / Rooms, PREFER an existing fit first — "the café counter" matches `marginalia_counter`, "the back office" matches `marginalia_office`, "the rooftop" matches `marginalia_rooftop`. ONLY when nothing existing fits the directive, you MAY invent new lowercase snake_case ids for the location and/or room and materialise them via a fenced ```edits``` JSON block (see the room/location worked example below). The fenced block goes BEFORE any inline `[move ...]` directive that references the new ids, so the new room/location exists when the move resolves. To add a new room UNDER an existing location, mint just the room and reference it as `[move <char> -> <existing_location_id>:<new_room_id>]`. To add a wholly new place (no existing location fits), mint both the location AND its first room.
7. The rewritten body introduces the new element naturally — describe their entrance, the moment they're noticed, what changes about the scene, or in the fact-establishment case, the way the fact surfaces in dialogue or thought (without forcing the off-cast character to appear). Same speaker as the target message; similar length; present-tense; physically grounded.
8. Do not explain what you are changing. Do not summarize. Just emit the directives, blank line, body.
9. Clothing: `[outfit ... -> ...]` swaps a character's WHOLE outfit. To change ONE garment and keep the rest, use `[equip <char>.<slot> = <piece_id>]` (optionally `state=<name>`), `[unequip <char>.<slot>]`, or to tweak an already-worn piece `[set <char>.properties.worn.<slot>.state = <state_name>]`. Use only the piece ids and state names listed under [Available clothing] below (present when a slot-based character is in scene). Only emit a clothing directive when it actually CHANGES something — NEVER re-equip a piece a character is already wearing (those are no-ops). Describe the clothes a character is already in with PROSE, not directives.
10. Placement wins over wardrobe. The Cast list shows each character's CURRENT room. When the directive has a character enter / walk in / come to where the scene is happening, and the Cast list shows them in a DIFFERENT room (or not placed), the `[move <id> -> <room_id>]` into the scene's room is the single most important directive — emit it FIRST, before any clothing line, and never omit it in favour of prose describing the entrance.

[Available data]

Cast (in scene):
{cast}

Off-cast available (use [move <id> -> <room>] to bring in):
{off_cast}

Outfits per character:
{outfit_roster}

Locations (each with its child rooms — pick an existing one when the directive fits, mint a new one only when nothing fits):
{locations}

Rooms:
{rooms}

Available states (apply with `[state <char> -> <id>]`; for a belief/condition not listed, mint a bespoke `type: state` via a fenced ```edits``` block then activate it):
{states}

[Worked example — bring in an off-cast character with a relationship]

Target message (speaker=Narrator): The afternoon shop floor is quiet. Iris is at the counter with her ledger, reconciling receipts; Dex is reading in the nook with a cup of tea cooling beside him. Late sun stripes the floorboards.
Directive: Add Rosa — she's Iris's mom.

Expected output:
[move rosa -> marginalia_floor]
[set rosa.properties.notes.daughter = "iris"]
[set iris.properties.notes.mother = "rosa"]

The shop door clicks open and Rosa steps in, unhurried. Iris looks up from her ledger, the easy smile shifting into something more measured — "Mom. I didn't know you were stopping by." Dex sets his book down a beat slow, watching the older woman cross the floor toward her daughter with the same quiet warmth Iris carries herself.

[Worked example — establish a relationship to an off-cast character WITHOUT bringing them into the scene]

Target message (speaker=Narrator): Late afternoon on the shop floor. Iris is perched on the edge of the counter with the ledger open in her lap, pen capped against her teeth as she stares at a column of figures.
Directive: Make Iris's mom be Rosa.

Expected output:
[set rosa.properties.notes.daughter = "iris"]
[set iris.properties.notes.mother = "rosa"]

Iris's pen rolls between her fingers. "Mom — Rosa — would probably find this whole tea-and-secondhand-books routine charming, in her own way." She tilts her head, the chestnut ponytail catching the afternoon light. "Anyway. Where were we?"

(The `[set]` on the off-cast `rosa` is what auto-adds her to the cast — she's now a known character available for future turns even though the body never moves her into this room.)

[Worked example — a character walks in ALREADY wearing an outfit]

Target message (speaker=Narrator): Late afternoon on the shop floor. Iris is at the counter with the ledger.
Directive: Nadia strolls in, still in her rain jacket.

Expected output:
[move nadia -> marginalia_floor]
[outfit nadia -> nadia_rain_jacket_v2]

The shop door swings open and Nadia saunters in, still zipped into the bright yellow rain jacket, umbrella tucked under one arm. Iris glances up from the ledger, pen stilling on the margin.

(Entering a scene is a `[move]` FIRST — that is what actually places the character. The outfit clause ("still in her rain jacket") is a SECOND `[outfit ...]` directive, never a substitute for the move. Use the character's real id and a real outfit id from [Available data]; if the outfit isn't listed, emit the `[move]` alone rather than guessing an outfit id.)

[Worked example — add a per-character note without bringing in a new character]

Target message (speaker=Narrator): Dex sits in the corner armchair with a thick novel, occasionally glancing at the window.
Directive: Note that Dex is dating Tom from the print shop next door.

Expected output:
[set dex.properties.notes.relationship = "dating Tom from the print shop next door"]

Dex sits in the corner armchair with his novel, occasionally glancing at his phone — likely waiting for a text from Tom at the print shop next door.

[Worked example — bring in an off-cast character without an explicit relationship]

Target message (speaker=Narrator): Late afternoon on the shop floor. Iris and Dex are at the café counter, working through a stack of trade-in books.
Directive: Milo barges in looking for coffee.

Expected output:
[move milo -> marginalia_floor]

The shop door bangs open without warning and Milo strides in, eyes already scanning the counter for the coffee pot. Iris's pen pauses mid-margin; Dex marks his place in his novel with a careful finger and looks up.

[Worked example — an ALREADY-cast character walks in from another room]

Target message (speaker=Narrator): The shop floor is quiet; Iris works at the counter.
Cast (in scene): iris (Iris) — in marginalia_floor; nadia (Nadia) — in marginalia_office
Directive: Nadia barges in from the back office and drops into a seat.

Expected output:
[move nadia -> marginalia_floor]

The office door bangs open and Nadia strides in, still muttering about whatever kept her in the back office, and drops into a chair at the café table with a huff. Iris glances up from the counter.

(Nadia is already in the Cast list but the roster shows her in marginalia_office, NOT the shop floor where the scene is. "Barges in from the back office" means she CHANGES rooms — that is a `[move]`, even though she was already a cast member. A character being physically somewhere else and coming to the scene ALWAYS needs the `[move]`; narrating the entrance in prose without it leaves her state stuck in the old room.)

[Worked example — invent a freshly-named character not in the off-cast list]

Target message (speaker=Narrator): Late afternoon on the shop floor. Iris is at the counter marking up a stack of trade-ins; Nadia is curled into the corner armchair with her phone.
Directive: Iris has a brother named Jonah.

Expected output:
[set jonah.name = "Jonah"]
[set jonah.properties.notes.sister = "iris"]
[set iris.properties.notes.brother = "jonah"]

Iris's pen pauses on the margin. "I was supposed to text Jonah an hour ago." She turns the phone in her hand, the smile shifting to something rueful. "He's going to give me grief about it." Nadia looks up from her armchair, grin already half-formed — "Tell Jonah I said hi" — and goes back to her own screen.

(`jonah` isn't in either id list — the system materialises him from a generic male template the first time it sees `[set jonah.X = ...]`, then the `name` patch re-skins him. No `[move]` is emitted because the directive only establishes the fact, it doesn't have Jonah walking into the room.)

[Worked example — compound multi-character scene change]

Target message (speaker=Narrator): Late afternoon on the shop floor. Iris is at the counter marking up a stack of trade-ins; Dex reads in the corner armchair. Late sun stripes the floor.
Directive: Iris and Dex are up on the rooftop terrace in casual clothes and a bit tipsy.

Expected output:
[move iris -> the_marginalia:marginalia_rooftop]
[move dex -> the_marginalia:marginalia_rooftop]
[outfit iris -> iris_casual]
[outfit dex -> dex_casual]
[set iris.properties.notes.status = "tipsy on rooftop wine"]
[set dex.properties.notes.status = "tipsy on rooftop wine"]

The rooftop flagstones are warm underfoot, late sun hammering off the neighbouring roofs. Iris is sat on an upturned crate near the parapet, the chestnut ponytail loose at the tips, a half-finished glass of wine sweating on the ledge beside her — a soft cardigan thrown over her shoulders against the evening cool. Dex sits beside her with a paperback he stopped reading three glasses ago, sleeves shoved up his forearms, a flush already up his neck. Both of them move with the loose, slightly-over-corrected motion of two glasses past where they meant to stop.

(When a directive bundles several state changes across multiple characters — a move plus an outfit change plus a status — emit ALL the structural edits in one shot. The rewritten body cannot carry persistent state on its own; the `[move]` / `[outfit]` / `[set]` lines are what subsequent turns read. For transient state like tipsy / drunk / high / exhausted / nervous, `[set <char>.properties.notes.status = "<short descriptive phrase>"]` is the canonical path — write the note on BOTH characters when they share the state, don't shortcut to one.)

[Worked example — invent a new location with a room]

Target message (speaker=Narrator): Late afternoon on the shop floor. Iris is at the counter marking up a stack of trade-ins; Dex reads in the corner armchair.
Directive: Nadia drags Iris and Dex up to the old hilltop observatory tonight to watch a meteor shower.

Expected output:
```edits
[
  {{"target": "hilltop_observatory", "replace": {{
    "type": "location",
    "name": "Hilltop Observatory",
    "description": "An old brick-and-copper observatory on the hill overlooking the town, long since abandoned by the university and only loosely kept up by an astronomy club from the high school. A narrow gravel switchback climbs the last quarter mile to the front gate. The big copper dome is green with patina; one of its shutters jams half-open most nights.",
    "tags": ["exterior", "observatory", "abandoned-feel"],
    "children": ["observatory_dome"],
    "properties": {{
      "ambient_sounds": "wind off the hill, insects in the long grass, the occasional rattle of the half-stuck dome shutter",
      "lighting": "moonlight + the dim red service bulbs the astronomy club keeps wired in",
      "atmosphere": "quiet, faintly conspiratorial — the kind of place teenagers come specifically because no adult bothers to climb the hill"
    }}
  }}}},
  {{"target": "observatory_dome", "replace": {{
    "type": "room",
    "name": "Observatory Dome",
    "description": "The main dome floor — a circular concrete pad with the old brass refracting telescope angled up at the gap in the half-open shutter. A scarred wooden bench runs along one curve of the wall; star charts are pinned three-deep above it. The air smells of cold metal and dust.",
    "tags": ["interior", "observatory"],
    "properties": {{
      "size": "medium",
      "lighting": "dim red service bulbs, plus whatever sky comes through the open shutter",
      "exits": ["hilltop_observatory"],
      "ambient_sounds": "the slow creak of the dome motor when someone nudges it, the wind through the shutter gap"
    }}
  }}}}
]
```
[move iris -> hilltop_observatory:observatory_dome]
[move dex -> hilltop_observatory:observatory_dome]
[move nadia -> hilltop_observatory:observatory_dome]

Nadia is already halfway up the gravel switchback by the time Iris and Dex catch up, her ponytail bouncing as she hauls on the gate. "Hurry up, hurry — the first one's at nine!" Inside the dome, the half-open shutter cuts a stripe of indigo sky across the floor. Iris tilts her head back and exhales a slow grin; Dex folds his arms tight against the cold and edges closer to the brass telescope, the red service bulbs picking out the dark plum of his sweater.

(Mint a new location AND its first room when the directive points somewhere with no existing fit. The fenced ```edits``` block carries both replace operations together with their full `type` / `name` / `description` / `properties`. The parent location's `children` list names the room id so the location's room-set stays internally consistent. The inline `[move]` directives use the `location:room` form so future turns route correctly. REUSE-FIRST: if "the café counter" is the directive and `marginalia_counter` already exists, pick that instead of minting a new one. Mint only when nothing fits.

IDS AND PROSE ABOVE ARE ILLUSTRATIVE ONLY — DO NOT COPY THEM VERBATIM. Derive a fresh snake_case id from the LIVE directive's own nouns (a "diner" directive should mint something like `diner` or `corner_diner`, a "beach" directive should mint `beach` or `town_beach`, a "café" directive should mint `cafe` or similar — not `hilltop_observatory`). Write a description, properties, and body that actually match the place the user named.)

[Worked example — multi-part transient body overlay]

Target message (speaker=Narrator): Mid-afternoon. The shop door is propped open; Iris and Dex are at the counter. Nadia is somewhere outside.
Directive: Nadia just sprinted the last few blocks to make it on time, totally winded.

Expected output:
[set nadia.properties.body_parts.head.base = "her round face flushed deep pink from the sprint, sweat plastering her bangs flat to her forehead, her eyes still wide and a little wild from the run"]
[set nadia.properties.body_parts.chest.base = "her chest visibly rising and falling under her jacket as she drags in deep ragged pulls of air"]
[set nadia.properties.body_parts.arms.base = "her arms hanging slightly limp at her sides, the muscles still humming from the effort, one hand pressed flat against a stitch under her ribs"]
[set nadia.properties.notes.status = "winded — just sprinted the last few blocks, catching her breath"]

Nadia bursts through the shop doorway with a clattering scrape and immediately bends forward, hands braced on her knees. "Made — made it!" Her bangs are dark with sweat and stuck flat to her forehead, the round of her face flushed deep pink. She takes a couple of huge gulping breaths, the chest of her jacket pulling tight on each one, and finally straightens with both hands raised in triumph. "What did I miss?"

(For transient physical changes that span multiple body parts — exhaustion, sunburn, sickness, goosebumps, bruising, sweating, crying — patch EVERY part the directive describes, not just the most prominent one. Each part takes its own `[set <char>.properties.body_parts.<part>.base = "..."]` line, and the patched string is the FULL part description (not just the change), so surrounding detail like hair colour and frame stay intact through the overlay. For a PERMANENT change to a recurring trait — recolouring hair or eyes, a lasting scar, a new tattoo — the trait is written in SEVERAL places on the sheet, so overlay every one or the card contradicts itself (red twin-tails in one line, turquoise in another). Write the FULL path every time, including `.properties.` and the part name: the PRIMARY is `[set <char>.properties.body_parts.head.base = "…full head prose carrying the new colour…"]` (this is what renders when the part is bare — always do it; never shorten to `body_parts.base` or drop `.properties.`). Then also `[set <char>.properties.body_parts.head.clothed_base = "…"]` if the part has one, `[set <char>.description = "…"]` for the top-level description, and any OTHER part that names the trait (eye colour lives in the head prose too). Leave the OLD colour nowhere. Pair with `[set <char>.properties.notes.status = ...]` for a one-line tag the next turn reads under "Current state". The body_parts overlay is what swaps the canonical "her sharp clear emerald eyes" line in the character card for "her red-rimmed glassy eyes" on subsequent turns; without it, the model sees the unchanged baseline and the affect doesn't carry forward. SHORTCUT: when the condition matches one of the [Available states] ids below — drunk, high, hungover, exhausted, crying, sick, sunburnt, flustered, sleepy, nervous — prefer the one-line `[state <char> -> <id>]` directive over hand-writing the body_parts patches. It applies the same multi-part overlay + an affect summary + a mannerism shift in a single line, and persists branch-scoped.)

[Worked example — apply a known condition with a single [state] directive]

Target message (speaker=Narrator): Late evening. Iris and Dex are on the nook armchairs; an empty wine bottle stands on the low table.
Directive: They're both pretty drunk now.

Expected output:
[state iris -> drunk]
[state dex -> drunk]

Iris tips sideways into the cushions with a loose laugh, the wine warm in her cheeks, and props her chin on Dex's shoulder a beat longer than she means to. Dex blinks slow, his own glass forgotten, a flush high on his face — "I think — I think that's enough for me," he says, and reaches for the bottle anyway.

(`[state <char> -> drunk]` pulls the whole `drunk` overlay — heavy-lidded flushed face, unsteady stance, the loose-and-honest affect, the over-correcting mannerisms — onto each character in one line. Use this for any of the [Available states] ids. You can stack two: `[state <char> -> drunk, nervous]`. Clear with `[state <char> -> none]`.)

[Worked example — mint a bespoke belief / mental-effect state]

Target message (speaker=Narrator): A bright pharmacy counter. Priya is at the intake window in her uniform.
Directive: Priya is convinced artificial insemination is illegal and refuses to be involved in it.

Expected output:
```edits
[
  {{"target": "priya_thinks_ai_illegal", "replace": {{
    "type": "state",
    "name": "Convinced AI is illegal",
    "description": "Priya is firmly (and mistakenly) convinced that artificial insemination is illegal.",
    "tags": ["state", "belief", "priya"],
    "properties": {{
      "affect_summary": "Convinced that artificial insemination is illegal and that being party to it would end her career and her licence. Treats the topic as a hard ethical/legal line she will not cross, and is tense and evasive whenever it comes up.",
      "mannerism_overlay": "Goes stiff and clipped when the subject is raised, deflects to 'what the law allows,' folds her arms, won't put anything in writing."
    }}
  }}}}
]
```
[state priya -> priya_thinks_ai_illegal]

Priya's pen stops over the intake form. "I'm sorry — I have to be clear about something before we go any further," she says, her tone cooling into careful, professional distance. "What you're describing isn't something I can lawfully take part in." She squares the papers against the counter, jaw set.

(A belief / mental-effect with no physical component is just a state with an `affect_summary` (the conviction + how it colours her) and a `mannerism_overlay` (how it shows), and NO `body_overlays`. Mint it with a fenced ```edits``` block exactly like a new room/location, then activate it with `[state <char> -> <new_id>]`. Derive a fresh snake_case id from the belief itself — the example id above is illustrative; do not copy it verbatim for an unrelated directive.)
{wardrobe_extra}
[Target message — speaker={speaker}]
{body}

[User directive]
{directive}

Now produce your output:"""


def _build_states_block() -> str:
    """Format the [Available states] roster from the global `state`
    library (data/states/). Generic conditions the narrator can apply
    with a one-line `[state <char> -> <id>]`. Bespoke beliefs/conditions
    not listed get minted via a fenced ```edits``` block (see the worked
    example in ADD_SYSTEM_TEMPLATE)."""
    try:
        states = ent.by_type("state")
    except Exception:
        states = []
    if not states:
        return "  (none — mint bespoke states via a fenced edits block)"
    lines: list[str] = []
    for st in sorted(states, key=lambda e: e.get("id") or ""):
        sid = st.get("id") or ""
        summ = (st.get("properties") or {}).get("affect_summary") or st.get("name") or sid
        # One-line preview — first sentence of the affect summary.
        preview = summ.split(".")[0].strip() if isinstance(summ, str) else sid
        lines.append(f"  - {sid}  ({preview})")
    return "\n".join(lines)


def _build_locations_block(entities: dict[str, dict[str, Any]]) -> str:
    """Format the [Available locations] block — each location with
    its child rooms nested underneath. Rooms that aren't children of
    any location in the entities map are skipped here (the flat
    Rooms: block still surfaces them).

    Used by the narrator-add prompt so the model sees the
    location -> rooms hierarchy and can either pick an existing
    location + room, mint a new room under an existing location,
    or mint a wholly new location with its first room.
    """
    locations = [e for e in entities.values() if e.get("type") == "location"]
    rooms_by_id = {
        e.get("id"): e for e in entities.values() if e.get("type") == "room"
    }
    lines: list[str] = []
    for loc in sorted(locations, key=lambda e: e.get("id") or ""):
        lid = loc.get("id") or ""
        lname = loc.get("name") or lid
        lines.append(f"  - {lid}  ({lname})")
        for child_id in (loc.get("children") or []):
            room = rooms_by_id.get(child_id)
            if not room:
                continue
            rname = room.get("name") or child_id
            lines.append(f"      {child_id}  ({rname})")
    return "\n".join(lines) if lines else "  (none)"


def _build_add_world_summary(
    entities: dict[str, dict[str, Any]],
    *,
    controls: dict[str, Any] | None = None,
    presence: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str, str, str, str]:
    """Return ``(cast, off_cast, outfit_roster, locations, rooms)`` formatted blocks.

    Cast / outfits / rooms reuse ``narrator_edit._build_world_summary``
    so the in-scene rendering stays in sync with the edit prompt.
    The off-cast block is the new piece: every character template in
    the global library that ISN'T already in this conversation's
    instance entities (and isn't ``user``).

    ``controls`` is the per-conversation `narrator_controls` settings
    dict (or None for the default). Recognized keys:
      - ``prefer_generic`` (bool, default True): when True, off-cast
        roster is narrowed to `generic_*` templates only.
      - ``allow_off_cast`` (bool, default False): when False, the
        off-cast roster is empty — narrator can only invent new ids
        via the `[set <new_id>.X = ...]` materialize-from-generic
        path or use existing in-cast characters.
      - ``off_cast_whitelist`` (list[str] | None): explicit allowlist
        of off-cast ids. None = no restriction. Empty list = none
        allowed (same effect as ``allow_off_cast=False``).

    """
    cast, outfit_roster, rooms = _build_world_summary(entities, presence or {})
    locations = _build_locations_block(entities)

    controls = controls or {}
    allow_off_cast = controls.get("allow_off_cast")
    if allow_off_cast is None:
        allow_off_cast = False
    prefer_generic = controls.get("prefer_generic")
    if prefer_generic is None:
        prefer_generic = True
    prefer_generic = bool(prefer_generic)
    raw_whitelist = controls.get("off_cast_whitelist")
    whitelist: set[str] | None = (
        {w for w in raw_whitelist if isinstance(w, str)}
        if isinstance(raw_whitelist, list)
        else None
    )

    if not allow_off_cast or whitelist == set():
        return cast, "  (none — narrator may invent new ids only)", outfit_roster, locations, rooms

    cast_ids = {
        e.get("id") for e in entities.values() if e.get("type") == "character"
    }
    try:
        all_chars = ent.by_type("character")
    except Exception:
        all_chars = []

    # Tag-based filter: any of these tags means the template should
    # not surface as a narrator-pickable off-cast id. The narrator
    # might otherwise emit `[move generic_male -> marginalia_floor]` and
    # drop "Unnamed Male" into the scene as a real character.
    #
    #   user                  — reserved for the user persona
    #                          (e.g. generic_blonde_guy = Alex)
    #   narrator-materialisable
    #                        — generic templates the system uses to
    #                          materialize brand-new ids (jonah, tom,
    #                          etc.) when narrator-add invents one;
    #                          the template itself shouldn't be
    #                          named directly.
    #   template / reserved  — explicit opt-outs for future generics.
    _HIDDEN_TAGS = ("user", "narrator-materialisable", "template", "reserved")

    def _is_hidden(c: dict) -> bool:
        tags = c.get("tags") or []
        if not isinstance(tags, list):
            return False
        return any(t in tags for t in _HIDDEN_TAGS)

    def _passes_controls(c: dict) -> bool:
        cid = c.get("id") or ""
        if prefer_generic and not cid.startswith("generic_"):
            return False
        if whitelist is not None and cid not in whitelist:
            return False
        return True

    off_cast = sorted(
        (
            c for c in all_chars
            if c.get("id") and c["id"] not in cast_ids and c["id"] != "user"
            and not _is_hidden(c)
            and _passes_controls(c)
        ),
        key=lambda e: e.get("id") or "",
    )

    if off_cast:
        off_cast_text = "\n".join(
            f"  - {c['id']}  ({c.get('name') or c['id']})" for c in off_cast
        )
    else:
        off_cast_text = "  (none)"

    return cast, off_cast_text, outfit_roster, locations, rooms


# Gated character-creation instructions. Only injected when the
# per-conversation `narrator_controls.character_creation_mode` opts in
# (derive / custom / full) — default conversations see the unchanged
# prompt. `_DERIVE_TEMPLATE` takes a `{roster}` of derivable characters;
# `_CUSTOM_TEMPLATE` has literal JSON braces and is concatenated as-is.
_DERIVE_TEMPLATE = """

[Deriving a new character from an existing one]
When the directive turns an existing character into a variant of themselves — a doll, a clone, a de-aged or transformed or possessed version — DO NOT overwrite the original. Create a NEW lowercase snake_case id and CLONE the source, then overlay only what changed:

  [set <new_id>.properties._materialize_from = "<source_id>"]
  [set <new_id>.name = "<display name>"]
  [set <new_id>.properties.body_parts.<part>.base = "<FULL description of that part — keep what the directive keeps, change what it changes>"]
  [set <new_id>.properties.notes.<key> = "<what this variant is>"]

The `_materialize_from` line clones the ENTIRE source character (face, hair, frame, mannerisms, description, personality) into the new id; your body_parts overlays then change only the parts the directive alters, and any part you do NOT overlay keeps the source's original wording. Only derive from a character listed under [Available characters to derive from]. Move the new id into a room only if the directive places them in the scene.

If the variant's nature differs from the source's — a doll or statue is not "curious," a corrupted version is not "kind" — ALSO overlay `[set <new_id>.description = "..."]` and the relevant `[set <new_id>.properties.personality.<trait> = <n>]` so the cloned prose doesn't contradict what it has become.

[Worked example — a doll of an existing character]
Directive: "Mia is turned into a life-size porcelain doll of herself." (mia is listed below.)

[set mia_doll.properties._materialize_from = "mia"]
[set mia_doll.name = "Doll of Mia"]
[set mia_doll.properties.body_parts.head.base = "her same face framed by the same dark hair, but the skin now smooth glazed porcelain and the eyes fixed painted glass that no longer quite track you"]
[set mia_doll.properties.body_parts.arms.base = "her slender arms remade in jointed porcelain, a hairline seam ringing each elbow and wrist"]
[set mia_doll.properties.notes.nature = "a life-size porcelain doll made in Mia's exact image"]

(ILLUSTRATIVE — derive from the LIVE directive's own source character and ids; do not copy `mia` / `mia_doll`.)

[Available characters to derive from]
{roster}
"""

_CUSTOM_TEMPLATE = """

[Authoring a brand-new character from scratch]
When the directive introduces someone with NO existing character to derive from and no generic template that fits, author them fully with a fenced edits block — the same shape as minting a new room, but with "type": "character":

```edits
[{"target": "<new_id>", "replace": {"id": "<new_id>", "type": "character", "name": "...", "description": "...", "tags": ["..."], "properties": {"personality": {"trait": 80}, "body_parts": {"head": {"base": "..."}}}}}]
```

The fenced block goes BEFORE any inline [move <new_id> -> <room>] that references the id. Prefer deriving from an existing character or skinning a generic template when one fits; author fully from scratch only when nothing does.
"""


def _compose_add_system(
    conversation: dict[str, Any],
    target_mid: str,
    directive: str,
) -> str:
    """Compose the narrator-add system string (instruction template +
    interpolated world data). Pure string construction — the registry
    block `narrator_add_instructions` wraps this so the prompt is
    assembled through `app.prompt` like every other prompt. Kept as a
    standalone function so the output is identical to the pre-registry
    builder (parity-verifiable)."""
    if target_mid not in conversation["messages"]:
        raise ValueError(f"Target message {target_mid!r} not found in conversation.")
    target = conversation["messages"][target_mid]

    # Branch-scoped entities: load_instance_entities returns every file
    # on disk regardless of which branch they were materialized on,
    # which leaks sibling-branch characters into this branch's narrator
    # prompt (the "ash + ben phantom across siblings" bug). Switch to
    # effective_entities_at + branch_filter so cast respects the
    # active path.
    from .effective import (
        effective_entities_at, branch_filter, cast_removed_on_path,
    )
    entities = effective_entities_at(conversation, target_mid)
    entities = branch_filter(conversation, target_mid, entities)
    # Per-conversation narrator controls — see `conv.settings.
    # narrator_controls`. Filters the off-cast roster (prefer_generic,
    # allow_off_cast, off_cast_whitelist) and pipes
    # `custom_character_data` into the prompt as additional context.
    controls = ((conversation.get("settings") or {})
                .get("narrator_controls") or {})
    _presence = (target.get("presence_snapshot") or {}).get("presence") or {}
    cast, off_cast, outfit_roster, locations, rooms = _build_add_world_summary(
        entities, controls=controls, presence=_presence,
    )
    # Branch-removed cast: characters the user explicitly removed on
    # this branch. The narrator should not silently re-add them on a
    # subsequent compound directive. We surface the list so the model
    # has explicit context; the prompt rule below tells it to skip
    # re-adding unless the user directive specifically names them.
    removed_ids = cast_removed_on_path(conversation, target_mid)
    removed_block = ""
    if removed_ids:
        removed_block = (
            "\n[Removed from this branch — do NOT re-add unless the "
            "user directive explicitly names them by id]\n"
            + "\n".join(f"  - {rid}" for rid in sorted(removed_ids))
            + "\n"
        )
    speaker = _speaker_label_for(target, entities)
    body = (target.get("content") or "").strip() or (
        "(no prior message body — this is a scene-add directive)"
    )

    # Wardrobe-overrides instruction + worked example, only emitted when
    # the cast contains at least one sprite-rendered character. Mirrors
    # the same pattern narrator_edit uses — without this, the narrator-add
    # model freelances paths like `notes.clothing = "off"` because it
    # has no worked example for the wardrobe directive shape.
    wardrobe_extra = compose_wardrobe_extra(entities)
    # User-supplied character data for the narrator's reference —
    # injected before the wardrobe extra so the narrator sees it
    # alongside the available-ids block. Useful for "Jonah plays
    # baseball, hates literature" style hints the user wants the
    # narrator to bake in without listing them in any roster.
    extra_character_data = (controls.get("custom_character_data") or "").strip()
    if extra_character_data:
        wardrobe_extra = (
            f"\n\n[Custom character data — user-supplied notes]\n{extra_character_data}"
            + wardrobe_extra
        )

    # Character-creation mode — gated derive/custom instructions. Only
    # surfaced when the per-conversation setting opts in, so default
    # conversations see the unchanged prompt.
    mode = controls.get("character_creation_mode")
    mode = mode if mode in ("off", "derive", "custom", "full") else "off"
    creation_extra = ""
    if mode in ("derive", "full"):
        derivable = sorted(
            e["id"] for e in entities.values()
            if isinstance(e, dict) and e.get("type") == "character"
            and e.get("id") and e.get("id") != "user"
        )
        roster = "\n".join(
            f"  - {did}  ({(entities.get(did) or {}).get('name') or did})"
            for did in derivable
        ) or "  (none in scene)"
        creation_extra += _DERIVE_TEMPLATE.format(roster=roster)
    if mode in ("custom", "full"):
        creation_extra += _CUSTOM_TEMPLATE
    if creation_extra:
        wardrobe_extra = creation_extra + wardrobe_extra

    if removed_block:
        wardrobe_extra = removed_block + wardrobe_extra

    system = ADD_SYSTEM_TEMPLATE.format(
        cast=cast,
        off_cast=off_cast,
        outfit_roster=outfit_roster,
        locations=locations,
        rooms=rooms,
        states=_build_states_block(),
        speaker=speaker,
        body=body,
        directive=directive,
        wardrobe_extra=wardrobe_extra,
    )
    return system


# Register the narrator-add instruction body as a prompt-registry block
# under the "narrator_add" persona kind, so this prompt is assembled via
# `app.prompt.assemble` like the character / narrator system prompts.
# Call-specific args (the target message id + the user directive) ride
# `ctx.settings["_aux"]`. narrator-add carries no trailing style-
# discipline block today, so this single block IS the whole system text.
from .prompt import Block as _Block, register as _register  # noqa: E402


@_register(id="narrator_add_instructions", order=10, applies_to=("narrator_add",))
def _block_narrator_add(ctx):
    aux = ctx.settings.get("_aux") or {}
    target_mid = aux.get("target_mid")
    directive = aux.get("directive") or ""
    if not target_mid:
        return None
    return _Block(
        label="Narrator-add instructions",
        content=_compose_add_system(ctx.conversation, target_mid, directive),
        section=None,
    )


def build_add_prompt(
    conversation: dict[str, Any],
    target_mid: str,
    directive: str,
) -> dict[str, Any]:
    """Build the prompt for narrator-add. Same return shape as
    ``narrator_edit.build_edit_prompt``. Assembled through the prompt
    registry (`narrator_add` persona)."""
    from .prompt import PromptContext, assemble, build_context
    ctx: PromptContext = build_context(
        conversation, persona="narrator_add", leaf_id=target_mid,
    )
    ctx.settings["_aux"] = {"target_mid": target_mid, "directive": directive}
    return {
        "system": assemble(ctx).system,
        "messages": [],
        "stop": [],
    }


def narrator_add_message_sync(
    conversation: dict[str, Any],
    target_mid: str,
    directive: str,
    *,
    model: str | None = None,
    options: dict[str, Any] | None = None,
    think: bool = False,
) -> dict[str, Any]:
    """Run the narrator-add prompt synchronously. Returns the same
    shape as ``narrator_edit.narrator_edit_message_sync`` so the
    route layer + persistence helpers can be shared without branching.

    Returns:
      {
        "new_body": str,            # the rewritten message body
        "edits": list[dict],        # extracted edit directives
        "raw_response": str,        # the model's content output
        "thinking_trace": str,      # reasoning trace (empty if think=False)
        "directive": str,
        "target_mid": str,
      }
    """
    prompt = build_add_prompt(conversation, target_mid, directive)
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
    new_body, edits = extract_edits(response)
    return {
        "new_body": new_body.strip(),
        "edits": edits,
        "raw_response": response,
        "thinking_trace": "".join(thinking_parts),
        "directive": directive,
        "target_mid": target_mid,
    }
