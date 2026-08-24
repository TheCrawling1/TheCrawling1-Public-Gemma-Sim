"""Modules: scenario modifiers selected at staging time.

A module is a reusable, branch-instanced overlay on a scenario. Each
module lives under ``data/modules/<id>/`` with a ``module.json`` manifest
(this V1 ships manifest-only modules; runtime hooks come later for code-
backed modules like RPG Control).

The manifest declares the module's id, display metadata, settings schema
(per-branch user-tweakable knobs the staging UI auto-generates a form
for), and any GUI controls the chat surfaces should render when the
module is active on the current branch's setup root.

Modules opt in via the scenario's top-level ``available_modules`` and
per-setup ``default_modules`` lists. When the user starts a Scene
staging branch, the panel collects ``modules`` + ``module_settings``
from the user and stamps them on the new root's metadata, where they
ride along with the rest of the path-replay state — identical isolation
semantics to setups.

The frontend reads the manifests (via ``GET /api/modules``), reads the
active list on the setup root (``metadata.modules``), reads the per-
branch settings (``metadata.module_settings``), and renders the
relevant controls. No Python hooks are dispatched in V1.
"""
from __future__ import annotations

import json
import os
from typing import Any

from flask import current_app


_MANIFESTS: dict[str, dict[str, Any]] | None = None


def _modules_dir() -> str:
    return os.path.join(current_app.config["data_dir"], "modules")


def _load_manifest_file(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    mid = raw.get("id")
    if not isinstance(mid, str) or not mid:
        return None
    if raw.get("type") != "module":
        return None
    raw.setdefault("name", mid)
    raw.setdefault("description", "")
    raw.setdefault("tags", [])
    raw.setdefault("requires", [])
    raw.setdefault("conflicts", [])
    raw.setdefault("settings", [])
    raw.setdefault("contributes", {})
    return raw


def _discover() -> dict[str, dict[str, Any]]:
    root = _modules_dir()
    out: dict[str, dict[str, Any]] = {}
    if not os.path.isdir(root):
        return out
    for entry in sorted(os.listdir(root)):
        sub = os.path.join(root, entry)
        manifest_path = os.path.join(sub, "module.json")
        if not os.path.isfile(manifest_path):
            continue
        m = _load_manifest_file(manifest_path)
        if m:
            out[m["id"]] = m
    return out


def all_manifests() -> dict[str, dict[str, Any]]:
    """Return the discovered manifest table, loading from disk on first call."""
    global _MANIFESTS
    if _MANIFESTS is None:
        _MANIFESTS = _discover()
    return _MANIFESTS


def reload() -> None:
    """Drop the cache so the next call to ``all_manifests`` re-reads disk."""
    global _MANIFESTS
    _MANIFESTS = None


def get(module_id: str) -> dict[str, Any] | None:
    return all_manifests().get(module_id)


def list_for_scenario(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the manifest list every staging panel offers to the user.

    Modules are universally available — every registered manifest shows
    up regardless of the scenario's content. Authors can still use a
    setup's ``default_modules`` to pre-check specific modules in the
    staging panel for that setup; the legacy ``scenario.available_modules``
    field is honored for the ORDERING (those ids float to the top) but
    is no longer a whitelist.
    """
    manifests = all_manifests()
    raw = scenario.get("available_modules")
    declared_order: list[str] = []
    if isinstance(raw, list):
        declared_order = [m for m in raw if isinstance(m, str) and m in manifests]
    seen = set(declared_order)
    out: list[dict[str, Any]] = [manifests[m] for m in declared_order]
    for mid, manifest in manifests.items():
        if mid in seen:
            continue
        out.append(manifest)
    return out


def default_setting_values(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a dict mapping setting.id -> setting.default for a manifest.
    Used as the seed for ``metadata.module_settings.<id>`` when staging
    a branch that activates this module and the user hasn't customized
    its knobs."""
    out: dict[str, Any] = {}
    for s in manifest.get("settings") or []:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        out[sid] = s.get("default")
    return out


def coerce_settings(
    manifest: dict[str, Any], raw: Any
) -> dict[str, Any]:
    """Validate + type-coerce user-supplied settings against the manifest.

    Returns a dict containing exactly the manifest's declared setting
    ids, defaulted from the manifest when the user didn't supply a
    value. Unknown keys are dropped silently. Used by the route layer
    when persisting ``metadata.module_settings.<id>``.
    """
    if not isinstance(raw, dict):
        raw = {}
    out: dict[str, Any] = {}
    for s in manifest.get("settings") or []:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        if sid in raw:
            out[sid] = _coerce_value(raw[sid], s)
        else:
            out[sid] = s.get("default")
    return out


def _coerce_value(value: Any, schema: dict[str, Any]) -> Any:
    t = schema.get("type")
    if t == "bool":
        return bool(value)
    if t == "int":
        try:
            n = int(value)
        except (TypeError, ValueError):
            return schema.get("default")
        lo = schema.get("min")
        hi = schema.get("max")
        if isinstance(lo, int) and n < lo:
            n = lo
        if isinstance(hi, int) and n > hi:
            n = hi
        return n
    if t == "enum":
        opts = schema.get("options") or []
        if value in opts:
            return value
        return schema.get("default")
    # "char_ref", "str", anything else: pass through if string-ish
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return schema.get("default")


def filter_active(active_ids: Any, _available_ids: list[str] | None = None) -> list[str]:
    """Return the user's active_ids in declaration order, filtered down
    to ids that resolve to a registered manifest. Modules are universally
    available, so the legacy `available_ids` argument is ignored — kept
    in the signature for callsite compatibility."""
    if not isinstance(active_ids, list):
        return []
    manifests = all_manifests()
    seen: set[str] = set()
    out: list[str] = []
    for mid in active_ids:
        if not isinstance(mid, str) or mid in seen or mid not in manifests:
            continue
        seen.add(mid)
        out.append(mid)
    return out
