# Architecture

A tour of how GemmaSim turns a folder of JSON into a live, stateful roleplay
scene. This is the deeper companion to the overview in the [README](../README.md).

## The core idea

A character front-end usually sends the model one static "character card" plus
the chat history. GemmaSim instead keeps a small **world** and *composes* the
prompt for every turn from the current state of that world. The character card
the model sees on turn 40 is not the same text it saw on turn 1 — it reflects
where the character now is, what they're wearing, who else is present, and any
state overlays that have been applied since.

Three ideas do most of the work:

1. **Entities** — the world is plain JSON, loaded into one flat map.
2. **Effective state** — a character's *live* description is resolved by
   layering overrides on top of their base definition.
3. **Composition** — each turn's prompt is assembled from registered blocks
   that read that live state.

## 1. Entities

Everything in `data/` is an *entity*: a JSON object with an `id`, a `type`
(`character`, `location`, `room`, `scenario`, `outfit`, `state`, `object`,
`lore`, `module`, ...), and a `properties` bag. `app/entities.py` validates
them and `app/storage.py` loads them with a single recursive scan of `data/`.
An entity is keyed by its `id` and placed by its `type`, so the directory
layout is a convention for humans, not something the loader depends on.

A **scenario** is the entity that ties a scene together: it lists the
`characters` to instance, the `locations` in play, a `starting_state` (which
room each character begins in, and what they're wearing), `first_messages`, an
`opening_prompt`, and which `personas` get a turn (the characters plus the
literal `narrator`).

## 2. Effective state

A character's base JSON is the *default*. During play, the narrator and the
engine apply changes — a character moves rooms, swaps an outfit, picks up a
`nervous` state. Rather than mutate the base entity, these are stored as
overrides and resolved on read.

`app/effective.py`, `app/layers.py` and `app/merge.py` implement this: given a
character and the current conversation branch, they produce the *effective*
character — base description, with wardrobe composition (`app/clothing_v2.py`)
and any active `state` overlays merged in. This is what keeps a character
consistent turn to turn while still reacting to what's happened.

State overlays (`data/states/*.json`) are reusable affect bundles — `nervous`,
`exhausted`, `drunk`, etc. — each carrying an `affect_summary`, a
`mannerism_overlay`, and optional per-part `body_overlays`. Applying one is a
single narrator directive: `[state <char> -> nervous]`.

## 3. Prompt composition

`app/prompt/` is a small registry + pipeline. Registered blocks each know how
to render one part of the prompt (identity, appearance, who else is present,
the surroundings, recent memory, the scenario framing, wardrobe notes, ...).
`app/personas.py` builds the character and narrator "cards" from the effective
state; `app/prompt/core.py` orders and assembles the blocks into the final
message list, and `app/ollama_client.py` streams the completion from Ollama.

Because composition reads live state, adding a feature that influences the
prompt is usually a matter of registering a new block or filter — which is
exactly what modules do.

## The narrator

The narrator is a persona with a job the characters don't have: staging scenes
and changing the world. It emits a small **directive grammar** that the engine
parses and applies:

```
[move <char> -> <room>]          move a character between rooms
[outfit <char> -> <outfit_id>]   swap a whole outfit
[equip <char>.<slot> = <piece>]  change one garment
[state <char> -> <state_id>]     apply an affect overlay
[set <entity>.<path> = <value>]  write an arbitrary fact/note
```

`app/narrator_add.py`, `narrator_edit.py` and `narrator_apply.py` handle
introducing new elements, editing existing beats, and applying the parsed
directives back onto conversation state.

## Conversations and branches

`app/conversations.py` models a conversation as a tree: every character-facing
state is a branch-local snapshot, so branching off an earlier point resets the
scene to how it was there. `app/rbd.py` builds a franchise-neutral
checkpoint/rewind seam on top of that — rewind the world to a checkpoint while
the player keeps what they learned.

## Modules (the plugin system)

`app/modules/loader.py` discovers every `data/modules/<id>/` with a manifest and
imports its backend Python by file path at startup, inside an app context.
Module code runs its registrations at import time — prompt blocks, prompt
filters, GUI controls, extra routes — and a failing module logs a warning
rather than crashing the engine. `app/prefabs/` is a sibling loader for
"prefab" staging kinds.

Two clean example modules ship in this repo:

- **Texting** (`data/modules/texting/`) — a prompt *filter*: when a character
  replies to a text message, environmental blocks (surroundings, who else is
  present, wardrobe) are stripped from their prompt so they don't "see" the
  room. A good, small example of a module shaping the composed prompt.
- **Locked Image** (`data/modules/locked_image/`) — a GUI module that pins a
  character image to the top of the chat column as history scrolls. A good
  example of a module contributing front-end (JS/CSS) and a GUI control slot.

## The web app

`app/__init__.py` is the Flask app factory: it loads config, sets up
filesystem-backed sessions and a persisted secret key, enforces an optional IP
allowlist on every request, and registers the blueprints in `app/routes/`
(pages, JSON API, the streaming endpoint, sprite serving, and the module/prefab
mounts). `app/templates/` + `app/static/` are the chat UI and the **Studio** —
an in-browser authoring surface for characters, locations, rooms, scenarios,
outfits and objects.
