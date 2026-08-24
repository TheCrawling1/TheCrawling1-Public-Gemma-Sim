"""An unspeakable secret — a fact the player physically cannot communicate.

Franchise-neutral core mechanism for a "sealed secret" constraint: once the
player is *sealed*, any attempt to reveal the secret to a present listener is
BLOCKED by the rules — the words never reach the listener — and a fixed
escalation ladder advances (a warning, then worse, then worst). This module owns
the STATE and the RESOLUTION; it decides nothing by vibe. The narration only ever
reacts to the mechanical result, exactly like pf1e: the model narrates the choke,
it does not decide whether the choke happens.

No flavour here (no in-world lore). The consequence text per tier is supplied
by the caller (the return_by_death module carries the themed wording). Like every
other loop-state layer this is a branch-local snapshot on the message tree, so it
is Return-by-Death-safe for free: rewinding past the seal un-seals, rewinding past
an attempt restores the earlier tier.
"""
from __future__ import annotations

from typing import Any

_META = "unspeakable"          # message.metadata.unspeakable = {focal: {...}}
MAX_TIER = 2                    # 0 = warning, 1 = worse, 2 = worst (capped)


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


def _write(conversation: dict[str, Any], focal: str, rec: dict[str, Any],
           leaf_id: str | None) -> dict[str, Any] | None:
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    msg = (conversation.get("messages") or {}).get(leaf_id)
    if not isinstance(msg, dict):
        return None
    msg.setdefault("metadata", {}).setdefault(_META, {})[focal] = rec
    return rec


def state(conversation: dict[str, Any], focal: str = "user",
          leaf_id: str | None = None) -> dict[str, Any]:
    """The nearest seal state for ``focal`` on the active path, leaf→root.
    ``{"active": bool, "tier": int, "attempts": int, "secret": str|None}``.
    Defaults to inactive when nothing on the path has sealed the focal."""
    for msg in _walk_up(conversation, leaf_id):
        rec = ((msg.get("metadata") or {}).get(_META) or {}).get(focal)
        if isinstance(rec, dict):
            return {"active": bool(rec.get("active")),
                    "tier": int(rec.get("tier") or 0),
                    "attempts": int(rec.get("attempts") or 0),
                    "secret": rec.get("secret")}
    return {"active": False, "tier": 0, "attempts": 0, "secret": None}


def is_sealed(conversation: dict[str, Any], focal: str = "user",
              leaf_id: str | None = None) -> bool:
    return state(conversation, focal, leaf_id).get("active", False)


def seal(conversation: dict[str, Any], focal: str = "user", *,
         secret: str | None = None, leaf_id: str | None = None) -> dict[str, Any] | None:
    """Place ``focal`` under the constraint on the message at ``leaf_id`` (default
    the active leaf). Idempotent: re-sealing an already-sealed focal on the same
    branch keeps the current tier/attempts (doesn't reset progress). Branch-local,
    so a rewind past this point un-seals."""
    cur = state(conversation, focal, leaf_id)
    rec = {"active": True,
           "tier": cur["tier"] if cur["active"] else 0,
           "attempts": cur["attempts"] if cur["active"] else 0,
           "secret": secret if secret is not None else cur.get("secret")}
    return _write(conversation, focal, rec, leaf_id)


def unseal(conversation: dict[str, Any], focal: str = "user",
           leaf_id: str | None = None) -> dict[str, Any] | None:
    """Lift the constraint (rare — the story explicitly frees them)."""
    return _write(conversation, focal,
                  {"active": False, "tier": 0, "attempts": 0, "secret": None}, leaf_id)


def attempt_reveal(conversation: dict[str, Any], *, focal: str = "user",
                   listeners: list[str] | None = None,
                   leaf_id: str | None = None) -> dict[str, Any]:
    """Resolve an attempt by ``focal`` to reveal the secret to ``listeners``.

    The whole decision is deterministic:
      * not sealed → ``{"blocked": False}`` — the words carry normally.
      * sealed → ``{"blocked": True, "tier": N, "attempts": M, "listeners": [...]}``
        and the new (advanced, capped) state is written onto ``leaf_id``.

    The rules block the communication and advance the escalation ladder; the
    caller renders the tier-N beat and guarantees the secret's content never
    reaches a listener's prompt. This function never touches narration.
    """
    cur = state(conversation, focal, leaf_id)
    if not cur["active"]:
        return {"blocked": False, "tier": 0, "attempts": cur["attempts"], "listeners": listeners or []}
    attempts = cur["attempts"] + 1
    tier = min(attempts - 1, MAX_TIER)
    _write(conversation, focal,
           {"active": True, "tier": tier, "attempts": attempts, "secret": cur.get("secret")},
           leaf_id)
    return {"blocked": True, "tier": tier, "attempts": attempts, "listeners": list(listeners or [])}
