"""Return by Death — rewind the world to a checkpoint while the player keeps
what they learned.

See docs/return_by_death_design.md. The heavy lifting is free: every
character-facing state is a branch-local snapshot, so branching off a checkpoint
resets NPCs/scene automatically and the dead loop stays in the tree as a sibling
subtree. This module adds the three pieces that aren't free — checkpoints, the
rewind operation, and the one asymmetry: the PLAYER's memory is carried forward
while every NPC's state resets.

Franchise-neutral core. Nothing here is franchise- or module-specific.
"""
from __future__ import annotations

from typing import Any

from . import conversations as convs
from . import memory as _memory
from .effective import memory_for_path


def set_checkpoint(conversation: dict[str, Any], leaf_id: str | None = None,
                   *, label: str = "", now: int = 0) -> str | None:
    """Mark a message as a save point. Return by Death rewinds to the nearest
    checkpoint on the active path. Returns the message id, or None if missing."""
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    msg = (conversation.get("messages") or {}).get(leaf_id)
    if not isinstance(msg, dict):
        return None
    msg.setdefault("metadata", {})["checkpoint"] = {
        "id": leaf_id, "label": label, "created_at": now}
    return leaf_id


def _walk_up(conversation: dict[str, Any], leaf_id: str | None):
    msgs = conversation.get("messages") or {}
    cur = msgs.get(leaf_id or conversation.get("active_path_leaf") or "")
    seen: set[str] = set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        yield cur
        if not cur.get("parent_id"):
            break
        cur = msgs.get(cur["parent_id"])


def active_checkpoint(conversation: dict[str, Any], leaf_id: str | None = None) -> str | None:
    """The nearest checkpoint ancestor on the active path, else the path root
    (so a scenario with no explicit checkpoint still rewinds to its opening)."""
    root = None
    for msg in _walk_up(conversation, leaf_id):
        if isinstance((msg.get("metadata") or {}).get("checkpoint"), dict):
            return msg["id"]
        root = msg["id"]
    return root


def loop_number(conversation: dict[str, Any], leaf_id: str | None = None) -> int:
    """Which life the active path is on (nearest `metadata.loop`, default 1)."""
    for msg in _walk_up(conversation, leaf_id):
        n = (msg.get("metadata") or {}).get("loop")
        if isinstance(n, int):
            return n
    return 1


def return_by_death(conversation: dict[str, Any], *, narrator_text: str | None = None,
                    now: int = 0) -> dict[str, Any] | None:
    """Die and rewind. Spawns a fresh loop branching off the nearest checkpoint;
    the dead loop stays in the tree (labelled, not deleted). The world resets for
    free (branch-local reads at the checkpoint); the PLAYER's memory is carried
    forward. Returns the new loop-root message, or None if there's nothing to do.

    The rule for what survives is a clean scope line: carry forward ONLY state
    scoped to the player (`focal == "user"`); everything scoped to an NPC resets.
    """
    leaf = conversation.get("active_path_leaf")
    if not leaf or leaf not in (conversation.get("messages") or {}):
        return None
    cp = active_checkpoint(conversation, leaf)
    if not cp:
        return None

    # Read the player's accumulated (already consolidated) memory BEFORE branching.
    player_facts = memory_for_path(conversation, "user", leaf)
    cur_loop = loop_number(conversation, leaf)

    # Mark the dead tip so the transcript can render "— Life N ended (death) —".
    dead_tip = conversation["messages"][leaf]
    dead_tip.setdefault("metadata", {})["loop_end"] = {"reason": "death", "at": now, "loop": cur_loop}

    # Spawn the new loop as a child of the checkpoint — a sibling of the dead
    # continuation. The checkpoint's presence carries the scene back; NPC state
    # reads at this node resolve to the checkpoint (the dead loop is off-path).
    cp_msg = conversation["messages"][cp]
    text = narrator_text or (
        "Return by Death. The world snaps back, unmade — and no one else remembers.")
    new_root = convs.append_message(
        conversation, parent_id=cp, persona="narrator", content=text,
        speaker_id=None, presence_snapshot=cp_msg.get("presence_snapshot"),
        metadata={"loop": cur_loop + 1, "return_by_death": True, "from_leaf": leaf,
                  # The loop-transition beat is player/GM-facing only. NPCs must
                  # never read that time reset — hidden from character prompts (the
                  # human still sees it in the transcript; the narrator still sees
                  # it). Half of the Witch's constraint: the loop stays the
                  # player's secret.
                  "hidden_from_characters": True},
    )

    # Carry the player forward: their resolved facts, re-stamped on the new
    # loop-root. NPC-scoped state (relationships/acquaintance/memory/beliefs) is
    # NOT copied — it resets to the checkpoint.
    for rec in player_facts:
        _memory.remember(conversation, "user", rec.get("text") or "",
                         where=rec.get("where"), source="carried", leaf_id=new_root["id"])

    conversation["active_path_leaf"] = new_root["id"]
    return new_root
