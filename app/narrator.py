"""Narrator-driven world-state edits.

The Narrator can express edits in three ways inside its message:

1. Whitelisted directive lines (one per line, on their own line):
       [move <character_id> -> <room_id>]
       [move <character_id> -> <location_id>:<room_id>]
       [outfit <character_id> -> <outfit_id>]
       [set <entity_id>.<dotted.path> = <value>]
       [unset <entity_id>.<dotted.path>]
   Values for `set` may be JSON (`true`, `42`, `"text"`, `[1,2]`) or a bare
   word (treated as a string).

2. A fenced ```edits ... ``` JSON block (escape hatch for arbitrary edits):
       ```edits
       [
         {"target": "iris", "patch": {"properties": {"current_outfit": "iris_casual"}}},
         {"target": "marginalia_floor", "replace": {...}}
       ]
       ```

3. Backwards compatibility: `{"edits": [{"id": ..., "patch": ...}]}` inside
   a generic ```json``` block (the original format).

extract_edits() returns (cleaned_prose, edits) where each edit is one of:

  {"kind": "patch",   "id": <entity_id>, "data": {<merge dict>}}
  {"kind": "replace", "id": <entity_id>, "data": {<full entity>}}
  {"kind": "unset",   "id": <entity_id>, "path": [<keys...>]}
  {"kind": "move",    "character_id": str, "room": str, "location"?: str}
  {"kind": "outfit",  "character_id": str, "outfit_id": str}

The route layer is responsible for applying edits and recording undo info.
"""
from __future__ import annotations

import json
import re
from typing import Any


_JSON_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_EDITS_FENCE = re.compile(r"```edits\s*(\[.*?\]|\{.*?\})\s*```", re.DOTALL)
_DIRECTIVE_LINE = re.compile(r"^\s*\[([^\[\]\n]+)\]\s*$", re.MULTILINE)

_VALID_DIRECTIVES = {"move", "outfit", "equip", "unequip", "set", "unset", "next", "state", "cast"}


def extract_edits(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Return (cleaned_prose, edits)."""
    edits: list[dict[str, Any]] = []
    cleaned = text

    # 1) `edits` fenced JSON blocks (preferred).
    cleaned, fenced = _consume(cleaned, _EDITS_FENCE, _parse_edits_block)
    edits.extend(fenced)

    # 2) Legacy `json` fenced blocks shaped like {"edits": [...]}
    cleaned, legacy = _consume(cleaned, _JSON_FENCE, _parse_legacy_json)
    edits.extend(legacy)

    # 3) Directive lines.
    cleaned, dirs = _consume_lines(cleaned, _DIRECTIVE_LINE, _parse_directive)
    edits.extend(dirs)

    return cleaned.strip(), edits


# ---------------------------------------------------------------------------
# Internal: regex consumers
# ---------------------------------------------------------------------------


def _consume(text: str, pattern: re.Pattern, parser) -> tuple[str, list[dict[str, Any]]]:
    """Strip every match of `pattern` from `text`, parsing each match into
    zero or more edit dicts via `parser(match_group_1)`."""
    out: list[dict[str, Any]] = []
    parts: list[str] = []
    last = 0
    for m in pattern.finditer(text):
        parts.append(text[last : m.start()])
        last = m.end()
        try:
            parsed = parser(m.group(1))
        except Exception:
            parsed = []
        if parsed:
            out.extend(parsed)
    parts.append(text[last:])
    return "".join(parts), out


def _consume_lines(text: str, pattern: re.Pattern, parser) -> tuple[str, list[dict[str, Any]]]:
    """Same as `_consume` but the matched line is removed (newline included)."""
    out: list[dict[str, Any]] = []
    parts: list[str] = []
    last = 0
    for m in pattern.finditer(text):
        parts.append(text[last : m.start()])
        last = m.end()
        # Eat a trailing newline so we don't leave a blank line behind.
        if last < len(text) and text[last] == "\n":
            last += 1
        try:
            parsed = parser(m.group(1).strip())
        except Exception:
            parsed = []
        if parsed:
            out.extend(parsed)
    parts.append(text[last:])
    return "".join(parts), out


# ---------------------------------------------------------------------------
# Block parsers
# ---------------------------------------------------------------------------


def _parse_edits_block(blob_text: str) -> list[dict[str, Any]]:
    """`[{...}, {...}]` or `{"edits":[...]}`. Each entry must have a target
    plus exactly one of patch / replace."""
    blob = json.loads(blob_text)
    if isinstance(blob, dict) and isinstance(blob.get("edits"), list):
        items = blob["edits"]
    elif isinstance(blob, list):
        items = blob
    else:
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        target = it.get("target") or it.get("id")
        if not target:
            continue
        if isinstance(it.get("patch"), dict):
            out.append({"kind": "patch", "id": target, "data": it["patch"]})
        elif isinstance(it.get("replace"), dict):
            out.append({"kind": "replace", "id": target, "data": it["replace"]})
    return out


def _parse_legacy_json(blob_text: str) -> list[dict[str, Any]]:
    blob = json.loads(blob_text)
    if not isinstance(blob, dict):
        return []
    return _parse_edits_block(blob_text) if isinstance(blob.get("edits"), list) else []


def _parse_directive(body: str) -> list[dict[str, Any]]:
    """Parse a single directive body (without the surrounding brackets).

    Supported forms:
      move <char> -> <room>
      move <char> -> <location>:<room>
      outfit <char> -> <outfit>
      set <entity>.<dotted.path> = <value>
      unset <entity>.<dotted.path>
    """
    # Extract verb + args. The verb is alphabetic; optionally followed
    # by ":" (the natural form `[next: dex]`), then whitespace and the
    # rest. Tolerates `[next dex]`, `[next: dex]`, and `[next:dex]`
    # equivalently. Other verbs are pure-alpha too, so this matches
    # them cleanly.
    m = re.match(r"^([A-Za-z]+)\s*:?\s*(.*)$", body)
    if not m:
        return []
    verb = m.group(1).lower()
    args = m.group(2).strip()
    if verb not in _VALID_DIRECTIVES:
        return []

    if verb == "move":
        m = re.match(r"^(\S+)\s*->\s*(.+)$", args)
        if not m:
            return []
        char_id = m.group(1)
        target = m.group(2).strip()
        if ":" in target:
            loc, room = target.split(":", 1)
            return [{"kind": "move", "character_id": char_id, "location": loc.strip(), "room": room.strip()}]
        return [{"kind": "move", "character_id": char_id, "room": target}]

    if verb == "outfit":
        m = re.match(r"^(\S+)\s*->\s*(\S+)$", args)
        if not m:
            return []
        return [{"kind": "outfit", "character_id": m.group(1), "outfit_id": m.group(2)}]

    if verb == "equip":
        # [equip <char>.<slot> = <piece_id>]
        # [equip <char>.<slot> = <piece_id> state=<state_name>]
        # Single-slot equip — replaces whatever was in worn[slot] without
        # touching other slots. Lets the narrator mix-and-match (equip
        # cage while keeping uniform; swap bra under same outfit; etc.).
        # Compiles down to a `patch` edit on properties.worn.<slot> —
        # deep-merge does the per-slot replace and leaves other slots
        # untouched.
        m = re.match(
            r"^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\s*=\s*(\S+)(?:\s+state\s*=\s*([A-Za-z0-9_]+))?\s*$",
            args,
        )
        if not m:
            return []
        char_id = m.group(1)
        slot = m.group(2)
        piece_id = m.group(3)
        state = m.group(4) or "on"
        return [{
            "kind": "patch",
            "id": char_id,
            "data": {"properties": {"worn": {slot: {"piece": piece_id, "state": state}}}},
        }]

    if verb == "unequip":
        # [unequip <char>.<slot>] — remove the piece in that slot, leave
        # other slots untouched. Compiles to an `unset` edit on
        # properties.worn.<slot>.
        m = re.match(r"^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\s*$", args)
        if not m:
            return []
        char_id = m.group(1)
        slot = m.group(2)
        return [{
            "kind": "unset",
            "id": char_id,
            "path": ["properties", "worn", slot],
        }]

    if verb == "set":
        m = re.match(r"^([A-Za-z0-9_]+)\.(\S+?)\s*=\s*(.+)$", args)
        if not m:
            return []
        entity_id = m.group(1)
        path = m.group(2).split(".")
        raw_value = m.group(3).strip()
        try:
            value = json.loads(raw_value)
        except Exception:
            # Fall back to bare-string: strip optional quotes.
            v = raw_value
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            value = v
        # Build a nested dict matching the dotted path so apply_patch can
        # shallow-merge it into the entity.
        nested: dict[str, Any] = value
        for key in reversed(path):
            nested = {key: nested}
        return [{"kind": "patch", "id": entity_id, "data": nested}]

    if verb == "unset":
        m = re.match(r"^([A-Za-z0-9_]+)\.(\S+)$", args)
        if not m:
            return []
        entity_id = m.group(1)
        path = m.group(2).split(".")
        return [{"kind": "unset", "id": entity_id, "path": path}]

    if verb == "state":
        # [state <char> -> <state_id>]
        # [state <char> -> <id_a>, <id_b>]   (multiple at once)
        # [state <char> -> none]             (clear all active states)
        # Replace semantics (like [outfit]): the directive fully
        # specifies the character's active_states list. Compiles to a
        # patch on properties.active_states so it rides path-replay and
        # stays branch-scoped.
        m = re.match(r"^(\S+)\s*->\s*(.+)$", args)
        if not m:
            return []
        char_id = m.group(1)
        rhs = m.group(2).strip()
        if rhs.lower() in ("none", "clear", "[]"):
            state_ids: list[str] = []
        else:
            state_ids = [s.strip() for s in rhs.split(",") if s.strip()]
        return [{
            "kind": "patch",
            "id": char_id,
            "data": {"properties": {"active_states": state_ids}},
        }]

    if verb == "cast":
        # Cast-membership directive — declare a temp cast member or soft-
        # remove one, branch-scoped (rides path-replay like every other
        # edit). Add/place is normally done with `[move ...]` (which pairs
        # a cast_add); this verb exists for the two cases move can't
        # express: a soft-remove, and an add-without-placing.
        #   [cast remove <char>]          soft-remove from this branch's cast
        #   [cast add <char>]             add to cast (no room change)
        #   [cast add <char> -> <room>]   add + place (delegates to move)
        m = re.match(r"^(add|remove|rm|del|delete)\s+(.+)$", args, re.IGNORECASE)
        if not m:
            return []
        op = m.group(1).lower()
        rest = m.group(2).strip()
        if op in ("remove", "rm", "del", "delete"):
            cid = rest.split()[0] if rest else ""
            return [{"kind": "cast_remove", "id": cid}] if cid else []
        # add — optional `-> room` placement reuses the move machinery.
        place = re.match(r"^(\S+)\s*->\s*(.+)$", rest)
        if place:
            char_id = place.group(1)
            target = place.group(2).strip()
            if ":" in target:
                loc, room = target.split(":", 1)
                return [{"kind": "move", "character_id": char_id,
                         "location": loc.strip(), "room": room.strip()}]
            return [{"kind": "move", "character_id": char_id, "room": target}]
        return [{"kind": "cast_add", "id": rest.split()[0]}] if rest else []

    if verb == "next":
        # Turn-handoff directive — declares who should speak next when
        # turn_mode=auto. Accept either bare id (`[next: dex]`) or
        # without the colon (`[next dex]`); the regex tolerates both
        # since the verb has already been stripped at this point.
        m = re.match(r"^:?\s*(\S+)\s*$", args)
        if not m:
            return []
        return [{"kind": "next", "id": m.group(1)}]

    return []
