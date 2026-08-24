"""Natural-language → custom user persona.

Build a structured `user` entity from a free-text self-description so the
user is a full character card (per-part body, personality, an outfit)
rather than the legacy `{name, description}` blurb. Mirrors
`setup_from_directive`: the narrator is given a tight prompt and asked to
emit `[set user.X = ...]` edits + an `[outfit user -> <id>]` pick, which
the route replays onto a new staging branch via
`conversations.seed_setup_root_from_directive`.

Why this rides the existing rails:

  - `[set user.properties.body_parts.<part>.base = "..."]` is
    branch-scoped (path-replay), so a custom user is per-branch — pick a
    different one on a sibling branch and they don't collide.
  - The prompt's `user_persona` block already renders a FULL character
    card when `properties.body_parts` is present
    (`app/prompt/core.py` — `has_structured_user`). Populating body_parts
    is what upgrades the user from blurb to card.
  - `[outfit user -> <generic_id>]` resolves through the generic outfit
    pool (now dual-format v1+v2), so it also populates `user.worn` and
    renders slot-based.

The prompt is assembled through `app.prompt` (persona "user_build") like
every other prompt.
"""
from __future__ import annotations

from typing import Any

from .entities import load_instance_entities
from .narrator import extract_edits


USER_BUILD_SYSTEM_TEMPLATE = """\
You are creating a custom player-character (the "user") for an interactive roleplay scene from a single free-text self-description. Translate the description into world-state directives that build the user as a fully-realised character.

Output format — directives only, one per line, no prose, no explanation:

1. Identity:
   [set user.name = "Proper Name"]
   [set user.description = "one or two sentences — who they are, their vibe"]
   [set user.role = "short role/occupation phrase"]

2. Body — one line PER body part, each value the FULL prose description of that part (not just a change). Cover every part the description implies; infer the rest plausibly from the persona. Parts:
   [set user.properties.body_parts.head.base = "..."]
   [set user.properties.body_parts.chest.base = "..."]
   [set user.properties.body_parts.arms.base = "..."]
   [set user.properties.body_parts.midriff.base = "..."]
   [set user.properties.body_parts.back.base = "..."]
   [set user.properties.body_parts.waist.base = "..."]
   [set user.properties.body_parts.legs.base = "..."]
   [set user.properties.body_parts.feet.base = "..."]
   [set user.properties.body_parts.hands.base = "..."]
   (Add armpits / anus when relevant. Each base is self-contained prose — hair colour, build, skin, distinguishing marks — because it replaces the body baseline wholesale.)

3. Personality — a few traits:
   [set user.properties.personality.<trait> = "<short value>"]

4. Outfit — pick ONE from the [Generic outfits] list that fits the description (the system pulls the template in on first use):
   [outfit user -> <generic_outfit_id>]

Rules:
- ONLY emit directives, each on its own line. No opening prose, no commentary, no fenced blocks.
- Always emit the identity triple (name / description / role) and at least head + chest + hands + legs body parts.
- Match the requested gender and body type. For a male user, describe a male figure and pick a male-appropriate generic outfit (e.g. `nude_male`) when no outfit is implied.
- Keep each value tight and concrete. Write the body description plainly and directly.

[Generic outfits] (any can be applied to the user):
{outfits}

[Self-description]
{description}

Now produce the directives:"""


def _generic_outfit_roster() -> str:
    """List the generic / templatable outfits the user can wear, by id.
    These are the data/outfits/*.json entries tagged generic/templatable
    — the universal pool any character (including the user) can pull."""
    from . import entities as ent
    try:
        outfits = ent.by_type("outfit")
    except Exception:
        outfits = []
    lines: list[str] = []
    for o in sorted(outfits, key=lambda e: e.get("id") or ""):
        tags = o.get("tags") or []
        if not isinstance(tags, list):
            continue
        if "generic" in tags or "templatable" in tags:
            name = o.get("name") or o.get("id")
            lines.append(f"  - {o.get('id')}  ({name})")
    return "\n".join(lines) if lines else "  (none)"


def _compose_user_build_system(description: str) -> str:
    """Compose the user-build instruction string. Pure string
    construction; the registry block `user_build_instructions` wraps it."""
    return USER_BUILD_SYSTEM_TEMPLATE.format(
        outfits=_generic_outfit_roster(),
        description=description.strip(),
    )


# Registry block — assembled through app.prompt like every other prompt.
from .prompt import Block as _Block, register as _register  # noqa: E402


@_register(id="user_build_instructions", order=10, applies_to=("user_build",))
def _block_user_build(ctx):
    aux = ctx.settings.get("_aux") or {}
    description = aux.get("description") or ""
    return _Block(
        label="User-build instructions",
        content=_compose_user_build_system(description),
        section=None,
    )


def build_user_prompt(
    conversation: dict[str, Any],
    description: str,
) -> dict[str, Any]:
    """Build the prompt for user-build. Same shape as `assemble_prompt`
    (system + messages + stop). Assembled through the prompt registry
    (`user_build` persona)."""
    from .prompt import PromptContext, assemble, build_context
    ctx: PromptContext = build_context(conversation, persona="user_build")
    ctx.settings["_aux"] = {"description": description}
    return {
        "system": assemble(ctx).system,
        "messages": [],
        "stop": [],
        "pieces": [],
    }


def parse_user_response(text: str) -> dict[str, Any]:
    """Parse the model's directive output into edits.

    Returns ``{"edits": [...], "leftover": str}``. Edits are in the
    `extract_edits` format and target the `user` entity (plus the outfit
    directive). The route replays them onto a new staging branch.
    """
    leftover, edits = extract_edits(text or "")
    return {"edits": edits, "leftover": leftover.strip()}
