"""Public API for prefab kind handlers.

Engine-provided builtin kinds AND data/ drop-in prefabs (a
``data/prefabs/<id>/prefab.py`` file) import from this surface only —
never from the staging routes. The contract:

  - ``register_kind(kind, handler)`` / ``BaseKind`` / ``PrefabContext``
    — register a staging-UI kind's behavior.
  - manifest helpers (``staging_ui_of``, ``staging_kind_of``,
    ``scenario_config``, ``composes_of``, ``get``) — read the manifest
    + scenario config.
  - data helpers (``substitute_template``, ``read_prefab_data``) —
    materialize per-character edit templates and read entity-side
    ``prefab_data`` blocks.
  - ``deep_merge`` — the same overlay merge the futa builtin uses.
  - ``object_catalog()`` — resolve every ``type=object`` entity.

A drop-in's ``prefab.py`` typically does, at import time::

    from app.prefabs.api import register_kind, BaseKind
    class MyKind(BaseKind):
        def build_panel(self, manifest, ui, cfg, ctx): ...
        def apply_picks(self, manifest, ui, cfg, pf_payload, ctx): ...
    register_kind("my_kind", MyKind())
"""
from __future__ import annotations

from typing import Any

from .registry import BaseKind, PrefabContext, register_kind  # noqa: F401
from . import (  # noqa: F401
    composes_of,
    get,
    read_prefab_data,
    scenario_config,
    staging_kind_of,
    staging_ui_of,
    substitute_template,
)
from ..merge import deep_merge  # noqa: F401


def object_catalog() -> dict[str, dict[str, Any]]:
    """Return ``{object_id: entity}`` for every ``type=object`` in the
    catalog. Used by object-pool kinds resolving a global pool."""
    from .. import entities as _ent_mod
    out: dict[str, dict[str, Any]] = {}
    for ent in _ent_mod.by_type("object"):
        oid = ent.get("id")
        if isinstance(oid, str) and oid:
            out[oid] = ent
    return out


def load_instance_or_template(cid: str | None, entity_id: str) -> dict[str, Any] | None:
    """Resolve an entity by id, preferring the conversation's instanced
    copy over the global template. Mirrors the lookup the futa builtin
    uses to read a character's current outfit at staging time."""
    from .. import entities as _ent_mod
    ent = None
    if cid:
        ent = _ent_mod.load_instance_entity(cid, entity_id)
    return ent or _ent_mod.get(entity_id)
