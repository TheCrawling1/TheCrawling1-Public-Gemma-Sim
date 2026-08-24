"""Natural-language → setup translator.

Build a setup root from a free-text directive. The user types something
like "Today is feature day, Iris and Dex in casual clothes, I'm from
the local paper here to take their picture." We feed that to the
narrator with a tight prompt that asks it to emit:

  1. State directives in the existing narrator-edit grammar
     ([outfit], [move], [set], [unset]) — one per line, on their own
     line. These get parsed by `extract_edits` and replayed on
     activation, exactly like an authored setup's `state` block.

  2. A fenced ```opening block with the root narrator message — the
     prose the user sees as the new branch's opening line.

  3. An optional fenced ```instructions block with extra rules for
     the setup (appended to the scenario's base scenario_instructions
     when this setup is active).

  4. An optional fenced ```name block with a short setup name for
     the chat UI's setup picker.

The route layer takes the parsed result and creates a new sibling
root on the conversation via `conversations.seed_setup_root`.
"""
from __future__ import annotations

import re
from typing import Any

from .entities import load_instance_entities
from .narrator import extract_edits
from .narrator_edit import _build_world_summary
from .personas import _style_discipline_block


SETUP_SYSTEM_TEMPLATE = """\
You are configuring a new branch of an interactive roleplay scene from a single free-text directive. Your job is to translate the directive into world-state directives + a fresh opening narration.

Output format (in this exact order):

1. State directives — one per line, on their own line, before any prose:
     [outfit <character_id> -> <outfit_id>]
     [move <character_id> -> <room_id>]
     [move <character_id> -> <location_id>:<room_id>]
     [state <character_id> -> <state_id>]
     [set <entity_id>.<dotted.path> = <value>]
     [unset <entity_id>.<dotted.path>]
   Use `[state <char> -> <id>]` for a condition or mental effect the
   premise implies — drunk, exhausted, flustered, nervous, etc. (see the
   [Available states] roster). For a belief or condition not in the
   roster (e.g. "she's convinced X is illegal"), mint a bespoke one with
   a fenced ```edits``` block (`{{"target": "<new_id>", "replace":
   {{"type": "state", "name": "...", "properties": {{"affect_summary":
   "...", "mannerism_overlay": "..."}}}}}}`) BEFORE the `[state]` line
   that activates it.
   Use only ids from the [Available data] block — never invent an id.
   If the directive describes a state that no listed id matches exactly
   (e.g. "casual clothes" when only `iris_apron` / `iris_smart`
   / `iris_weekend` are listed for that character), check the
   GENERIC outfit pool at the bottom of the outfit roster — any outfit
   listed there can be applied to any character (the system pulls the
   template into the instance on first use). Use the generic pool to
   cover vibes a character's owned outfits don't (e.g. swimwear / casual
   / loungewear / suit). Only fall back to the closest character-owned
   match if nothing in the generic pool fits either, and only omit the
   edit entirely if NOTHING is a plausible match — even then, cover the
   intent in the opening narration.
   Cast scope: only emit `[move]` for characters the directive says are
   *present* in the new scene. If the directive implies others are
   absent, leave them where they are — never invent a room id to "move
   them away."
   Special: `user` is a real entity. To set the user's name / role /
   description, write `[set user.name = "..."]`, `[set user.role = "..."]`,
   `[set user.description = "..."]`. To put them in a room, write
   `[move user -> <room_id>]`.

2. A fenced ```opening block with the root narrator message — present-tense,
   physically grounded, sets the scene. This is what the user sees as
   the opening line of the new branch.

3. An optional fenced ```instructions block with extra rules / context
   for this setup. Appended to the scenario's base scenario_instructions
   when this branch is active.

4. An optional fenced ```name block with a short label (≤ 5 words)
   for the chat UI's setup picker.

Worked example A — exact-match ids exist:
[outfit iris -> iris_apron]
[outfit dex -> dex_apron]
[move iris -> marginalia_floor]
[move dex -> marginalia_floor]
[move user -> marginalia_floor]
[set user.name = "Photographer"]
[set user.description = "From the local paper, dropping in for the shop feature photos."]
[set user.role = "newspaper photographer"]

```opening
The afternoon shop floor is golden with low sun. Iris looks up from the counter as the door opens, half-rising from her stool, while Dex marks his place in his novel with one finger. The user steps in with a camera bag over one shoulder.
```

```instructions
Today is feature day at The Marginalia. The user is from the local paper, here to get a photo of the bookshop and its regulars. Treat it as a brief, friendly working visit.
```

```name
Feature day
```

Worked example B — directive says "casual" but no `*_casual` id exists, so pick the closest available alternative:
Available outfits: `iris_apron`, `iris_smart`, `iris_weekend`
Directive: "Iris in casual clothes, on the shop floor. I'm a newspaper photographer."

[outfit iris -> iris_weekend]
[move iris -> marginalia_floor]
[move user -> marginalia_floor]
[set user.name = "Photographer"]
[set user.description = "Visiting from the local paper for the afternoon feature shoot."]
[set user.role = "newspaper photographer"]

```opening
The shop floor hums with the mid-afternoon quiet. Iris perches on the edge of the counter in her weekend clothes, kicking one heel against the stool's rung as she watches the door. The afternoon light catches the worn spines on the shelf behind her.
```

(`iris_weekend` is the closest non-apron option, so we pick it rather than skipping the outfit edit. The opening narration treats it as casual wear so the scene reads consistently. The user gets the full name + description + role triple — every setup should populate those so `{{user}}` expansion has something concrete to work with.)

Rules:
- Emit ALL directives FIRST, before any fenced block.
- Use blank lines around fenced blocks.
- Keep the opening tight (2-4 short sentences). It's a beat, not a chapter.
- Always populate the user persona triple — `[set user.name = "..."]`, `[set user.description = "..."]`, `[set user.role = "..."]` — even if the directive only hints at the user's identity. Pick reasonable defaults from context. Skipping any of the three leaves `{{user}}` expansion with placeholders.
- Only emit instructions / name when the directive justifies them.
- Don't explain what you're doing in your output. Don't summarize. Just produce the directives + fences.

[Available data]

Characters (the `user` entity is always available; refer to it as `user`):
{cast}

Outfits per character:
{outfit_roster}

Rooms:
{rooms}

Available states (apply with `[state <char> -> <id>]`; mint a bespoke one for beliefs/conditions not listed):
{states}

[User directive]
{directive}

Now produce your output:"""


_OPENING_FENCE = re.compile(r"```\s*opening\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_INSTRUCTIONS_FENCE = re.compile(r"```\s*instructions\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_NAME_FENCE = re.compile(r"```\s*name\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _compose_setup_system(
    conversation: dict[str, Any],
    directive: str,
) -> str:
    """Compose the setup-from-directive instruction string, WITHOUT the
    trailing style-discipline block (the registry's `style_discipline`
    block appends it). Uses `load_instance_entities` (all instance
    entities, matching the pre-registry behaviour — setup runs against
    the whole instance, not a branch path)."""
    entities = load_instance_entities(conversation["id"])
    cast, outfit_roster, rooms = _build_world_summary(entities)

    from .narrator_add import _build_states_block
    return SETUP_SYSTEM_TEMPLATE.format(
        cast=cast,
        outfit_roster=outfit_roster,
        rooms=rooms,
        states=_build_states_block(),
        directive=directive.strip(),
    )


# Register the setup instruction body as a prompt-registry block under
# the "setup" persona. The shared `style_discipline` block (order 190,
# applies_to includes "setup") appends the banned-phrase guidance —
# same output as the old `f"{system}\n\n{sd}"`.
from .prompt import Block as _Block, register as _register  # noqa: E402


@_register(id="setup_instructions", order=10, applies_to=("setup",))
def _block_setup(ctx):
    aux = ctx.settings.get("_aux") or {}
    directive = aux.get("directive") or ""
    return _Block(
        label="Setup-from-directive instructions",
        content=_compose_setup_system(ctx.conversation, directive),
        section=None,
    )


def build_setup_prompt(
    conversation: dict[str, Any],
    directive: str,
) -> dict[str, Any]:
    """Build the prompt for setup-from-directive. Same shape as
    `assemble_prompt` (system + messages + stop). Assembled through the
    prompt registry (`setup` persona)."""
    from .prompt import PromptContext, assemble, build_context
    ctx: PromptContext = build_context(conversation, persona="setup")
    ctx.settings["_aux"] = {"directive": directive}
    return {
        "system": assemble(ctx).system,
        "messages": [],
        "stop": [],
        "pieces": [],
    }


def parse_setup_response(text: str) -> dict[str, Any]:
    """Parse the model's response into structured setup data.

    Returns:
      {
        "edits":           list of edit dicts (extract_edits format),
        "opening_prompt":  string (root narrator message text),
        "instructions":    string (scenario_instructions_append),
        "name":            string (setup picker label),
        "leftover":        leftover prose after stripping the fences,
      }
    """
    body = text or ""

    opening = ""
    m = _OPENING_FENCE.search(body)
    if m:
        opening = m.group(1).strip()
        body = body[: m.start()] + body[m.end():]

    instructions = ""
    m = _INSTRUCTIONS_FENCE.search(body)
    if m:
        instructions = m.group(1).strip()
        body = body[: m.start()] + body[m.end():]

    name = ""
    m = _NAME_FENCE.search(body)
    if m:
        name = m.group(1).strip().splitlines()[0] if m.group(1).strip() else ""
        body = body[: m.start()] + body[m.end():]

    leftover, edits = extract_edits(body)

    return {
        "edits": edits,
        "opening_prompt": opening,
        "instructions": instructions,
        "name": name,
        "leftover": leftover.strip(),
    }
