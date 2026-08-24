"""Generic per-character fact memory — durable, branch-local knowledge.

See docs/character_memory_design.md. A character retains salient facts it has
learned (a password, a name, a task) as records stamped on the message tree:
RbD-safe, and ACCUMULATING along the path (the reader in
`effective.memory_for_path` unions every record leaf→root), so the character
keeps everything learned up to the active leaf and a rewind drops only what was
learned on the collapsed branch.

Franchise-neutral core: no world-sim, no module concepts. An optional module
(pf1e/AAM) may later WRITE here via the same seam — it never reaches a card.
"""
from __future__ import annotations

import re
from typing import Any

# How a fact was learned. Free-form is tolerated but these are the vocabulary.
# "carried" = brought across a Return-by-Death rewind (the player retains it).
VALID_SOURCES = {"witnessed", "told", "introduced", "projected", "carried"}


def normalize(text: str) -> str:
    """Loose key for dedup / supersession matching — case- and
    punctuation-insensitive, whitespace-collapsed."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def remember(
    conversation: dict[str, Any],
    focal_id: str,
    text: str,
    *,
    where: dict[str, Any] | None = None,
    source: str = "witnessed",
    supersedes: str | None = None,
    leaf_id: str | None = None,
) -> dict[str, Any] | None:
    """Record one fact `focal_id` now knows, on the message at `leaf_id`
    (default the active leaf). Idempotent on `text` for that message (won't
    double-add on a re-run). Returns the record, or None if the text is blank
    or the message is missing.

    `where` is optional provenance ({location, room}); `source` says how it was
    learned. The fact is branch-local, so it only reaches characters reading a
    path that includes this message — and a Return-by-Death rewind past it
    un-learns it."""
    text = (text or "").strip()
    if not focal_id or not text:
        return None
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    msg = (conversation.get("messages") or {}).get(leaf_id)
    if not isinstance(msg, dict):
        return None
    mem = msg.setdefault("metadata", {}).setdefault("memory", {})
    facts = mem.setdefault(focal_id, [])
    if any(isinstance(r, dict) and (r.get("text") or "").strip() == text for r in facts):
        return None
    rec: dict[str, Any] = {"text": text,
                           "source": source if source in VALID_SOURCES else "witnessed",
                           "leaf": leaf_id}
    if isinstance(where, dict) and where:
        rec["where"] = {k: where[k] for k in ("location", "room") if where.get(k)}
    if isinstance(supersedes, str) and supersedes.strip():
        # This fact replaces an earlier one (a password changed, a plan
        # updated). Recorded as a NEW append-only marker rather than editing
        # the old record — so a Return-by-Death rewind past this point restores
        # the old fact. `effective.memory_for_path` resolves it on read.
        rec["supersedes"] = supersedes.strip()
    facts.append(rec)
    return rec


# --------------------------------------------------------------------------- #
# Auto-capture — extract durable facts from turns aging out of a character's
# window (the truncation boundary). The model call is injected so this stays
# pure and testable; the route wraps it with threading + the conversation model.
# --------------------------------------------------------------------------- #
_FACT_CAP = 6
_SENTINELS = {"nothing", "none", "n/a", "no facts", "no memories", "-", "(none)"}


def _parse_facts(text: str, cap: int = _FACT_CAP) -> list[str]:
    """Turn an extractor's line-per-fact output into clean fact strings —
    strip bullets/numbering/quotes, drop empties and 'nothing' sentinels, cap
    and dedup (case-insensitive)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        # strip a leading bullet or "1." / "1)" marker
        while line[:1] in {"-", "*", "•"}:
            line = line[1:].strip()
        if line[:2].rstrip(".)").isdigit() and line[:1].isdigit():
            line = line.split(".", 1)[-1].split(")", 1)[-1].strip()
        line = line.strip().strip('"').strip("'").strip()
        if not line or len(line) < 3 or len(line) > 300:
            continue
        if line.lower().rstrip(".") in _SENTINELS:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= cap:
            break
    return out


_REPLACES = re.compile(r"\[replaces:\s*(.+?)\]\s*$", re.IGNORECASE)


def extract_facts(joined_witnessed: str, focal_name: str, chat_fn, *,
                  existing_facts: list[str] | None = None,
                  options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Ask the model which durable facts `focal_name` would retain from turns
    they witnessed and that are now leaving their memory — consolidation-aware.

    `existing_facts` (what the character already knows) lets the model skip
    duplicates and flag UPDATES: a returned fact may carry a `supersedes` naming
    the old fact it replaces (a password changed, a plan revised). `chat_fn(
    system, messages, options) -> str` is injected. Returns a list of
    `{"text": str, "supersedes": str|None}`; [] on empty input or model error
    (capture is best-effort and never raises into the turn)."""
    joined = (joined_witnessed or "").strip()
    if not joined:
        return []
    name = focal_name or "the character"
    system = (
        "You extract durable memories for a single character from a scene. You are "
        f"given messages that {name} witnessed and that are now leaving their "
        f"short-term memory. Output only the few FACTS {name} would genuinely "
        "retain: a name learned, a password or code, a task or promise, a place, a "
        "decision, an important event or revelation. One fact per line, each a "
        "short plain statement (e.g. 'The word that opens the study door is "
        "VERMILION.'). Skip small talk, ambient description, feelings, and anything "
        "trivial. If there is nothing worth remembering, output the single word "
        "NOTHING."
    )
    known = [f for f in (existing_facts or []) if isinstance(f, str) and f.strip()]
    if known:
        system += (
            "\n\n" + name + " ALREADY knows the following — do NOT repeat any of "
            "these:\n" + "\n".join(f"- {f}" for f in known) + "\nIf a new "
            "observation UPDATES or REPLACES one of them (e.g. a password or plan "
            "changed), write the new fact followed by ' [replaces: <the old fact "
            "verbatim>]'."
        )
    user = f"{name} witnessed:\n\n{joined}\n\nFacts {name} would remember:"
    try:
        out = chat_fn(system, [{"role": "user", "content": user}], options or {})
    except Exception:
        return []
    if not out or str(out).startswith("[ollama error"):
        return []
    records: list[dict[str, Any]] = []
    for line in _parse_facts(str(out)):
        m = _REPLACES.search(line)
        if m:
            old = m.group(1).strip().strip('"').strip("'").strip()
            text = line[:m.start()].strip()
            records.append({"text": text, "supersedes": old or None})
        else:
            records.append({"text": line, "supersedes": None})
    return [r for r in records if r["text"]]
