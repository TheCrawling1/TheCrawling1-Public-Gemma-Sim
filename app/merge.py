"""Deep-merge utilities for layered entity overrides.

`entities.apply_patch` only merges one level deep, which silently clobbers
sibling keys for nested patches like `properties.body_parts.head`. The
narrator-edit pipeline, scenario instancing, and the layered-editor save
path all need a recursive merge instead.

`UNSET_MARKER` lets a higher layer explicitly drop a key that lower layers
set — important for revert and for "remove this hat" semantics.
"""
from __future__ import annotations

import copy
from typing import Any


UNSET_MARKER = "__unset__"


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> None:
    """Recursively merge `patch` into `base` in place.

    Lists are replaced wholesale; dicts merge key-by-key; scalars overwrite.
    `UNSET_MARKER` values trigger a key removal."""
    for k, v in patch.items():
        if v == UNSET_MARKER:
            base.pop(k, None)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)


def deep_merged(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    deep_merge(out, patch)
    return out


def compute_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Return the sparse patch that turns ``old`` into ``new`` via ``deep_merge``.

    Dicts recurse; lists / scalars replace wholesale when changed; keys
    present in ``old`` but missing in ``new`` get ``UNSET_MARKER`` so the
    deep-merge consumer drops them. Unchanged keys are omitted.

    Used by the per-conv studio editor save path: the editor writes the
    full new entity, we diff against the path-effective entity at the
    active leaf, and append the diff as a ``kind=patch`` overlay onto the
    leaf — branch-scoped, no disk mutation. An empty result means no
    change and the caller should skip emitting an edit.
    """
    out: dict[str, Any] = {}
    for k, v in new.items():
        if k not in old:
            out[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(old.get(k), dict):
            sub = compute_diff(old[k], v)
            if sub:
                out[k] = sub
        elif v != old[k]:
            out[k] = copy.deepcopy(v)
    for k in old:
        if k not in new:
            out[k] = UNSET_MARKER
    return out


def slice_for_deep_patch(entity: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Capture the leaves a deep-merge patch will touch, marking absent keys
    with `UNSET_MARKER` so revert restores "this key was originally absent"."""
    out: dict[str, Any] = {}
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(entity.get(k), dict):
            out[k] = slice_for_deep_patch(entity[k], v)
        elif isinstance(v, dict):
            out[k] = UNSET_MARKER
        elif k in entity:
            out[k] = copy.deepcopy(entity[k])
        else:
            out[k] = UNSET_MARKER
    return out
