"""Generic, module-free relationship progression — the "getting to know you"
writer for the core relationship-tag channel.

The READ side lives in `effective.relationships_snapshot_for_path` +
`personas.resolve_relationship_tags`: pairs and the `relationship_standing`
prompt block gate on the focal's standing toward the user. This module is one
WRITER for that channel — it steps a focal's standing along a familiarity
ladder from a resolved check outcome and stamps a branch-local
`metadata.relationships` snapshot on a message (RbD-safe: it lives on the tree,
so a rewind reads the earlier standing).

It knows NOTHING about Pathfinder. It takes a settled `{success, margin}`
outcome — the same shape `pf1e_roll` already produces — so an optional module
(a Diplomacy social move) can drive it, and so can a plain in-fiction event or
a test. The vocabulary is the generic core `REL_VOCAB`; no attitude ladder or
module concept crosses in.
"""
from __future__ import annotations

from typing import Any

# The ordered "getting to know you" axis, cold → warm. A subset of REL_VOCAB;
# the romantic axis (crush/lover) and the raw-hostility tags are deliberately
# not part of the step ladder — those are set by events, not earned by a
# social check.
FAMILIARITY_LADDER = ["hostile", "wary", "stranger", "acquaintance", "friend", "close"]
_DEFAULT_STANDING = "stranger"


def _delta_from_outcome(outcome: dict[str, Any] | None) -> int:
    """Map a settled check outcome to a ladder step.

    success           → +1  (a solid connection made)
    success, margin≥10 → +2  (a standout — you really landed it)
    failure           →  0  (no progress, but no harm)
    failure, margin≤-10 → -1 (a real misstep — you set the rapport back)
    """
    if not isinstance(outcome, dict):
        return 0
    margin = outcome.get("margin") or 0
    if outcome.get("success"):
        return 2 if margin >= 10 else 1
    return -1 if margin <= -10 else 0


def current_standing(
    conversation: dict[str, Any] | None,
    focal_id: str,
    user_role: str = "",
    target: str = "user",
) -> str:
    """The focal's current ladder position toward `target` on the active path.

    Reads through the same resolver the pairs/standing use (explicit snapshot,
    else role seed). Picks the warmest ladder tag present; defaults to
    `stranger` when nothing on the ladder applies."""
    from .personas import resolve_relationship_tags
    tags = resolve_relationship_tags(focal_id, conversation, user_role) if target == "user" else set()
    present = [t for t in FAMILIARITY_LADDER if t in tags]
    return present[-1] if present else _DEFAULT_STANDING


def step_standing(current: str, delta: int) -> str:
    """Move `current` `delta` rungs along the ladder, clamped to the ends."""
    try:
        idx = FAMILIARITY_LADDER.index(current)
    except ValueError:
        idx = FAMILIARITY_LADDER.index(_DEFAULT_STANDING)
    idx = max(0, min(len(FAMILIARITY_LADDER) - 1, idx + delta))
    return FAMILIARITY_LADDER[idx]


def write_standing(
    conversation: dict[str, Any],
    focal_id: str,
    standing: str,
    *,
    target: str = "user",
    leaf_id: str | None = None,
) -> bool:
    """Stamp `metadata.relationships[focal][target] = [standing]` onto the
    message at `leaf_id` (default: the active leaf). Branch-local, so the
    standing applies from that message forward and a rewind past it reads the
    earlier value. Returns False if the message can't be found."""
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    msg = (conversation.get("messages") or {}).get(leaf_id)
    if not isinstance(msg, dict):
        return False
    rel = msg.setdefault("metadata", {}).setdefault("relationships", {})
    rel.setdefault(focal_id, {})[target] = [standing]
    return True


def mark_acquainted(
    conversation: dict[str, Any],
    focal_id: str,
    known: bool = True,
    *,
    target: str = "user",
    leaf_id: str | None = None,
) -> bool:
    """Record that ``focal_id`` now knows (or no longer knows) ``target``'s
    identity — an introduction (or its rewind). Stamps a branch-local
    ``metadata.acquaintance`` fact on the message at ``leaf_id`` (default the
    active leaf). Overrides the standing-derived default in
    ``personas.perceives_user_identity``. Returns False if the message is
    missing."""
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    msg = (conversation.get("messages") or {}).get(leaf_id)
    if not isinstance(msg, dict):
        return False
    acq = msg.setdefault("metadata", {}).setdefault("acquaintance", {})
    acq.setdefault(focal_id, {})[target] = bool(known)
    return True


def apply_check(
    conversation: dict[str, Any],
    focal_id: str,
    outcome: dict[str, Any] | None,
    *,
    target: str = "user",
    user_role: str = "",
    leaf_id: str | None = None,
) -> dict[str, Any]:
    """Apply a resolved check to the focal's standing and persist it.

    Returns `{old, new, delta, changed, line}`. `line` is a settled-fact
    sentence for narration, in the same 'narrate it, don't re-decide it'
    spirit as the roll line. Writes nothing extra when the standing doesn't
    move (a wasted check still records no change)."""
    old = current_standing(conversation, focal_id, user_role, target)
    delta = _delta_from_outcome(outcome)
    new = step_standing(old, delta)
    changed = new != old
    if changed:
        write_standing(conversation, focal_id, new, target=target, leaf_id=leaf_id)
    if delta > 0 and changed:
        line = f"{focal_id} warms toward {target}: {old} → {new}."
    elif delta < 0 and changed:
        line = f"{focal_id} cools toward {target}: {old} → {new}."
    else:
        line = f"{focal_id}'s standing toward {target} is unchanged ({old})."
    return {"old": old, "new": new, "delta": delta, "changed": changed, "line": line}
