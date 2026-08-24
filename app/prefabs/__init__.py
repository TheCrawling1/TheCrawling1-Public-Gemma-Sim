"""Prefabs: scenario-level staging-data overlays.

Prefabs are sibling to modules but live entirely at staging time —
they contribute UI sections to the scene-staging panel and the picks
land as ordinary edits on the new branch root. Once the branch is
spawned, prefabs do nothing else; no runtime hooks, no per-branch
settings, no GUI surface during chat.

Manifest layout::

    data/prefabs/<id>/prefab.json

Manifest shape (character-style, since v2)::

    {
      "id": "<prefab_id>",
      "type": "prefab",
      "name": "...",
      "description": "...",
      "tags": [...],
      "example_text": "",
      "children": [],
      "properties": {
        "staging_ui": {
          "kind": "<object_picker|per_character_toggle>",
          ...kind-specific fields
        },
        "scenario_config_schema": {...}
      }
    }

Legacy shape (pre-v2) is still readable — `contributes.scene_staging_section`
+ `scenario_schema` get normalized into the new keys by `_load_manifest_file`.

A scenario opts in via top-level ``available_prefabs: ["objects"]`` and
provides per-prefab config under ``prefabs.<id>: {...}``. The
staging-options route reads both and forwards the resolved data + the
manifest to the panel, which knows how to render each prefab kind.
"""
from __future__ import annotations

import json
import os
from typing import Any

from flask import current_app


_MANIFESTS: dict[str, dict[str, Any]] | None = None


def _prefabs_dir() -> str:
    return os.path.join(current_app.config["data_dir"], "prefabs")


def _load_manifest_file(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    pid = raw.get("id")
    if not isinstance(pid, str) or not pid:
        return None
    if raw.get("type") != "prefab":
        return None
    raw.setdefault("name", pid)
    raw.setdefault("description", "")
    raw.setdefault("tags", [])
    raw.setdefault("example_text", "")
    raw.setdefault("children", [])
    raw.setdefault("properties", {})

    # Legacy → new-shape normalization. Pre-v2 manifests put the UI
    # marker in `contributes.scene_staging_section` and the schema in
    # `scenario_schema`. Synthesize the v2 `properties.staging_ui` /
    # `properties.scenario_config_schema` so the rest of the system
    # can dispatch off a single shape.
    props = raw["properties"] if isinstance(raw["properties"], dict) else {}
    raw["properties"] = props
    if "staging_ui" not in props:
        contributes = raw.get("contributes") or {}
        section = contributes.get("scene_staging_section") if isinstance(contributes, dict) else None
        if section == "objects_picker":
            props["staging_ui"] = {"kind": "object_picker", "pool_source": "scenario_declared"}
    if "scenario_config_schema" not in props and isinstance(raw.get("scenario_schema"), dict):
        # The legacy `scenario_schema` was keyed by prefab id with a
        # nested `fields` block. The new shape drops the prefab-id key
        # since the schema lives on the prefab itself.
        legacy = raw["scenario_schema"]
        legacy_fields = None
        if isinstance(legacy, dict):
            for v in legacy.values():
                if isinstance(v, dict) and isinstance(v.get("fields"), dict):
                    legacy_fields = v["fields"]
                    break
        if legacy_fields:
            props["scenario_config_schema"] = {"fields": legacy_fields}
    return raw


def _discover() -> dict[str, dict[str, Any]]:
    root = _prefabs_dir()
    out: dict[str, dict[str, Any]] = {}
    if not os.path.isdir(root):
        return out
    for entry in sorted(os.listdir(root)):
        sub = os.path.join(root, entry)
        manifest_path = os.path.join(sub, "prefab.json")
        if not os.path.isfile(manifest_path):
            continue
        m = _load_manifest_file(manifest_path)
        if m:
            out[m["id"]] = m
    return out


def all_manifests() -> dict[str, dict[str, Any]]:
    global _MANIFESTS
    if _MANIFESTS is None:
        _MANIFESTS = _discover()
    return _MANIFESTS


def reload() -> None:
    global _MANIFESTS
    _MANIFESTS = None


def get(prefab_id: str) -> dict[str, Any] | None:
    return all_manifests().get(prefab_id)


def list_for_scenario(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Return manifests for each prefab the scenario opts into, in
    declaration order. Unknown ids are silently dropped."""
    raw = scenario.get("available_prefabs") or []
    if not isinstance(raw, list):
        return []
    table = all_manifests()
    out: list[dict[str, Any]] = []
    for pid in raw:
        if not isinstance(pid, str):
            continue
        m = table.get(pid)
        if m:
            out.append(m)
    return out


def scenario_config(scenario: dict[str, Any], prefab_id: str) -> dict[str, Any]:
    """Return the scenario's per-prefab config dict (or {})."""
    raw = scenario.get("prefabs")
    if not isinstance(raw, dict):
        return {}
    block = raw.get(prefab_id)
    if not isinstance(block, dict):
        return {}
    return block


# ---------------------------------------------------------------------------
# Manifest-shape helpers — read the v2 staging_ui block with legacy fallback.
# ---------------------------------------------------------------------------


def staging_ui_of(manifest: dict[str, Any] | None) -> dict[str, Any]:
    """Return the staging_ui dict for `manifest`, or `{}` if absent.

    v2 manifests put it under `properties.staging_ui`. Legacy manifests
    are normalized at load time by `_load_manifest_file`, so callers
    can rely on the v2 path here.
    """
    if not isinstance(manifest, dict):
        return {}
    props = manifest.get("properties")
    if not isinstance(props, dict):
        return {}
    ui = props.get("staging_ui")
    return ui if isinstance(ui, dict) else {}


def staging_kind_of(manifest: dict[str, Any] | None) -> str:
    """Return the staging_ui.kind for `manifest`, or ``""`` if unset."""
    ui = staging_ui_of(manifest)
    kind = ui.get("kind")
    return kind if isinstance(kind, str) else ""


def substitute_template(node: Any, mapping: dict[str, str]) -> Any:
    """Recursively walk a JSON-ish structure and apply ``{key}`` string
    substitutions from ``mapping``. Used by per_character_toggle to
    materialize per-character edits from a single template — e.g.
    ``"{character_id}"`` becomes the actual character id.

    Substitution applies to string VALUES anywhere in the structure;
    dict keys are not substituted. Non-string scalars pass through
    unchanged.
    """
    if isinstance(node, str):
        out = node
        for k, v in mapping.items():
            out = out.replace("{" + k + "}", v)
        return out
    if isinstance(node, dict):
        return {k: substitute_template(v, mapping) for k, v in node.items()}
    if isinstance(node, list):
        return [substitute_template(v, mapping) for v in node]
    return node


def composes_of(manifest: dict[str, Any] | None) -> list[str]:
    """Return the list of prefab ids this prefab composes (cascades
    into). A composing prefab fires its own staging edits AND each
    composed child's edits whenever it fires — composed children
    don't need to be in the scenario's available_prefabs list to
    cascade through a parent that is.
    """
    if not isinstance(manifest, dict):
        return []
    props = manifest.get("properties")
    if not isinstance(props, dict):
        return []
    raw = props.get("composes")
    if not isinstance(raw, list):
        return []
    return [pid for pid in raw if isinstance(pid, str) and pid]


def prefab_data_path(prefab_id: str, *extra: str) -> list[str]:
    """Return the dotted-path components for an entity's
    ``properties.prefab_data.<prefab_id>[/extra...]`` block. Used by
    handlers that look up character/outfit data scoped to a prefab —
    e.g. the futa per_character_toggle reads
    ``character.properties.prefab_data.futa``.
    """
    return ["properties", "prefab_data", prefab_id, *extra]


def read_prefab_data(
    entity: dict[str, Any] | None, prefab_id: str
) -> dict[str, Any] | None:
    """Return the per-prefab data block on ``entity``, or None if absent.
    Looks up ``entity.properties.prefab_data.<prefab_id>``."""
    if not isinstance(entity, dict):
        return None
    props = entity.get("properties")
    if not isinstance(props, dict):
        return None
    pd = props.get("prefab_data")
    if not isinstance(pd, dict):
        return None
    block = pd.get(prefab_id)
    return block if isinstance(block, dict) else None


# ---------------------------------------------------------------------------
# Staging-kind registry (drop-in dispatch)
# ---------------------------------------------------------------------------
#
# Re-export the registry surface so callers can do `prefabs.get_kind(...)`
# and `prefabs.register_kind(...)`. The staging routes dispatch every
# kind through this registry instead of hardcoding an `if kind == ...`
# chain — making prefabs drop-in: a new kind ships a handler, the engine
# never names it.

from .registry import (  # noqa: E402,F401
    BaseKind,
    PrefabContext,
    all_kinds,
    clear_kinds,
    get_kind,
    register_kind,
)

# Register the engine-provided builtin kinds via the public API. Imported
# at the BOTTOM so every helper function above is already defined before
# builtin_kinds (which pulls them in through app.prefabs.api) executes.
from . import builtin_kinds as _builtin_kinds  # noqa: E402,F401
