"""Map / navigation helpers — room pathfinding and follower resolution.

Pure, engine-level helpers used by the map panel route and the /move
endpoint. Movement is a *core* feature (not a module): every scenario
benefits, and the Pathfinder module builds on it.

Two jobs:

  * ``shortest_path`` — BFS over each room's ``properties.exits`` (a
    directed adjacency list already present on every room), so "go to
    room X" can walk the cleanest chain of prerequisite rooms instead of
    teleporting. Returns ``None`` when there's no exit-path (the caller
    then falls back to a direct move — an implicit "teleport").

  * ``follower_ids`` — the transitive set of characters whose
    ``properties.following`` chains up to a mover, so "if X changes rooms,
    their followers come too." Cycle-guarded; only present characters are
    swept (a follower who isn't in the scene doesn't teleport in).

Neither helper mutates state or calls the model — navigation costs no
response latency. Callers turn the results into presence updates / edits.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from . import entities as ent


def _room(cid: str, room_id: str) -> dict[str, Any] | None:
    """Resolve a room from the branch's instance dir, falling back to the
    global template catalog (global-only rooms aren't instanced until a
    move touches them)."""
    if not room_id:
        return None
    return ent.load_instance_entity(cid, room_id) or ent.get(room_id)


def room_exits(room: dict[str, Any] | None) -> list[str]:
    """The room's outgoing adjacency list (``properties.exits``), string
    ids only — EVERY declared exit, locked or not (for display)."""
    props = (room or {}).get("properties") or {}
    exits = props.get("exits")
    if not isinstance(exits, list):
        return []
    return [e for e in exits if isinstance(e, str) and e]


def locked_exits(room: dict[str, Any] | None) -> dict[str, Any]:
    """The still-locked exits of a room: ``{dest_room_id: {reason, skill, dc}}``.
    An entry with ``open`` truthy has been forced/unlocked and is dropped — so this
    is exactly the doors that currently block passage."""
    props = (room or {}).get("properties") or {}
    raw = props.get("locked_exits")
    if not isinstance(raw, dict):
        return {}
    return {d: info for d, info in raw.items()
            if isinstance(info, dict) and not info.get("open")}


def open_exits(room: dict[str, Any] | None) -> list[str]:
    """Exits you can actually walk right now — declared exits minus the ones still
    locked. Pathfinding uses this, so a barred door blocks the route until forced."""
    locked = locked_exits(room)
    return [e for e in room_exits(room) if e not in locked]


def shortest_path(cid: str, from_room: str | None, to_room: str | None) -> list[str] | None:
    """Return the shortest room chain ``[from_room, …, to_room]`` (inclusive)
    by BFS over ``exits``, or ``None`` if ``to_room`` is unreachable via
    exits.

    - ``to_room`` falsy → ``None`` (nowhere to go).
    - ``from_room`` falsy or equal to ``to_room`` → ``[to_room]`` (already
      there / no origin: a single-step move).

    Exits are treated as directed (they are authored that way); the BFS
    only ever follows declared exits, so an unreachable destination
    returns ``None`` and the caller can decide to move directly anyway.
    """
    if not to_room:
        return None
    if not from_room or from_room == to_room:
        return [to_room]
    seen = {from_room}
    queue: deque[list[str]] = deque([[from_room]])
    while queue:
        path = queue.popleft()
        for nxt in open_exits(_room(cid, path[-1])):
            if nxt in seen:
                continue
            if nxt == to_room:
                return path + [nxt]
            seen.add(nxt)
            queue.append(path + [nxt])
    return None


def distances_from(cid: str, from_room: str | None) -> dict[str, int]:
    """BFS hop-distance from ``from_room`` to every exit-reachable room.
    ``{from_room: 0, neighbour: 1, …}``. Empty when ``from_room`` is falsy.
    Used by the map panel to show how far each room is / grey out the
    unreachable ones."""
    if not from_room:
        return {}
    dist: dict[str, int] = {from_room: 0}
    queue: deque[str] = deque([from_room])
    while queue:
        cur = queue.popleft()
        for nxt in open_exits(_room(cid, cur)):
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                queue.append(nxt)
    return dist


def follower_ids(
    entities: dict[str, dict[str, Any]],
    leader_id: str,
    present_ids: set[str] | None = None,
) -> list[str]:
    """Return the transitive set of character ids that follow ``leader_id``.

    A character follows another when its ``properties.following`` equals the
    leader's id; chains resolve transitively (A→B→C all move when C moves)
    and cycles are guarded by the ``seen`` set. When ``present_ids`` is
    given, only characters currently in the scene are included — a follower
    who isn't present shouldn't be yanked into the room. Order is
    breadth-first from the leader (stable, deterministic).
    """
    seen = {leader_id}
    out: list[str] = []
    frontier: deque[str] = deque([leader_id])
    # Pre-index following → [followers] once so the sweep is linear.
    followers_of: dict[str, list[str]] = {}
    for cid_, e in entities.items():
        if not isinstance(e, dict) or e.get("type") != "character":
            continue
        tgt = (e.get("properties") or {}).get("following")
        if isinstance(tgt, str) and tgt:
            followers_of.setdefault(tgt, []).append(cid_)
    while frontier:
        cur = frontier.popleft()
        for f in sorted(followers_of.get(cur, [])):
            if f in seen:
                continue
            if present_ids is not None and f not in present_ids:
                continue
            seen.add(f)
            out.append(f)
            frontier.append(f)
    return out
