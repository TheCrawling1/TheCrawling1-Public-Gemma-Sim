"""Narrator pair-rewrite — a focused second-call pass.

Editing a character's ``dialogue_pairs`` doesn't fit the ``[set …]``
directive grammar (each pair is a ``{user, char}`` object and the list
doesn't deep-merge), and rewriting 8–10 examples inline would swamp the
main narrator turn. So, like ``life_sim`` and ``auto_state``, this is a
separate focused model call: given a character's CURRENT state (after the
main narrator edit has already applied), it rewrites the dialogue
examples so the replies sound like who the character is now, and returns
a single ``patch`` edit that replaces ``properties.dialogue_pairs``.

Gated per-branch by ``narrator_controls.edit_pairs``. Off by default.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .ollama_client import chat_sync


def is_active(conversation: dict[str, Any], leaf_id: str | None = None) -> bool:
    """True if ``narrator_controls.edit_pairs`` is on for this conversation."""
    controls = ((conversation.get("settings") or {}).get("narrator_controls") or {})
    return bool(controls.get("edit_pairs"))


# The `{…}` placeholders here are filled with str.replace (NOT .format),
# so the literal JSON braces in the instructions need no escaping.
_PAIRS_SYSTEM = """You are rewriting a roleplay character's dialogue examples so they match who the character is RIGHT NOW.

You are given the character's name, current description, personality, and their existing dialogue examples — each a pair of a user line and the character's reply. Something in the scene has changed this character; the description and personality you are given are the CURRENT state (a doll, a machine, drunk, transformed, aged, corrupted — whatever they have become).

Your job: rewrite the examples so the character's REPLIES sound like this current version of them — the voice, mannerisms, and physicality the current description implies. A lifeless porcelain doll answers haltingly or with hollow, doll-like stiffness; a drunk slurs; a machine speaks in flat protocol. Keep each user line usable (keep it or adjust it lightly), keep the SAME number of pairs and the same rough order of escalation.

Output ONLY a JSON array — no prose, no markdown fence:
[
  {"user": "<user line>", "char": "<the character's reply in their CURRENT voice>"},
  ...
]

Rules:
- Every reply must fit the CURRENT description/personality. Do not write them as the character used to be.
- If the current state says the character CANNOT speak (a doll that can't talk, mute, gagged, a machine that only beeps), their replies contain NO spoken words in quotes — only *actions*, silence, or a failed attempt to make a sound.
- Keep {{user}} / {{char}} macros if the originals use them.
- Replies may mix *action in asterisks* and "speech in quotes", like the originals.
- Same count as the originals. Valid JSON only.

Character: __NAME__

Current description:
__DESCRIPTION__

Current personality: __PERSONALITY__

Existing dialogue examples (rewrite every one):
__PAIRS__

Now output the rewritten JSON array only:"""


def _format_personality(char: dict[str, Any]) -> str:
    p = (char.get("properties") or {}).get("personality") or {}
    if not isinstance(p, dict) or not p:
        return "(none given)"
    return ", ".join(f"{k} {v}" for k, v in p.items())


def _existing_pairs(char: dict[str, Any]) -> list[dict[str, str]]:
    raw = (char.get("properties") or {}).get("dialogue_pairs") or []
    out: list[dict[str, str]] = []
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict) and isinstance(it.get("char"), str):
                out.append({"user": str(it.get("user") or ""), "char": it["char"]})
    return out


def build_pairs_prompt(char: dict[str, Any]) -> dict[str, Any]:
    """Assemble the pairs-rewrite side-call prompt for a character."""
    name = char.get("name") or char.get("id") or "the character"
    desc = (char.get("description") or "").strip() or "(no description)"
    pairs = _existing_pairs(char)
    pairs_json = json.dumps(pairs, ensure_ascii=False, indent=2) if pairs else "[]"
    system = (
        _PAIRS_SYSTEM
        .replace("__NAME__", name)
        .replace("__DESCRIPTION__", desc)
        .replace("__PERSONALITY__", _format_personality(char))
        .replace("__PAIRS__", pairs_json)
    )
    return {"system": system, "messages": []}


def _parse_pairs(raw: str) -> list[dict[str, str]]:
    """Pull the first JSON array of ``{user, char}`` objects out of the
    model output. Tolerant of a surrounding code fence or stray prose."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    text = raw.strip()
    # Strip a leading/trailing ``` fence if present.
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    candidates: list[str] = []
    # Whole thing, then the first [...] slice.
    candidates.append(text)
    lo = text.find("[")
    hi = text.rfind("]")
    if lo != -1 and hi != -1 and hi > lo:
        candidates.append(text[lo:hi + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, list):
            continue
        out: list[dict[str, str]] = []
        for it in data:
            if isinstance(it, dict) and isinstance(it.get("char"), str) and it["char"].strip():
                out.append({"user": str(it.get("user") or ""), "char": it["char"].strip()})
        if out:
            return out
    return []


def rewrite_pairs_sync(
    *,
    char: dict[str, Any],
    speaker_id: str,
    model: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the pairs-rewrite side call. Returns
    ``{"pairs": [...], "raw": str, "edit": <patch|None>}``. ``edit`` is a
    ready-to-apply patch replacing ``properties.dialogue_pairs``; None when
    the character has no existing pairs or the model returned nothing
    usable (so a bad call never wipes the character's examples)."""
    existing = _existing_pairs(char)
    if not existing:
        return {"pairs": [], "raw": "", "edit": None}
    prompt = build_pairs_prompt(char)
    try:
        raw = chat_sync(
            system=prompt["system"], messages=prompt["messages"],
            model=model, options=options or {}, think=False,
        )
    except Exception:
        return {"pairs": [], "raw": "", "edit": None}
    pairs = _parse_pairs(raw)
    if not pairs:
        return {"pairs": [], "raw": raw, "edit": None}
    edit = {
        "kind": "patch",
        "id": speaker_id,
        "data": {"properties": {"dialogue_pairs": pairs}},
    }
    return {"pairs": pairs, "raw": raw, "edit": edit}
