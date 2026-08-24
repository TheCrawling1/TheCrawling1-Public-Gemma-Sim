"""Auto-discover and import prefab backend code.

Each prefab under ``data/prefabs/<id>/`` may ship a ``prefab.py`` file
alongside its ``prefab.json`` manifest. When present, that file is
imported at engine startup — its top-level statements run (kind
registrations via ``register_kind``) and the prefab's behavior becomes
live. This is the prefab analogue of ``app.modules.loader``.

Engine-provided builtin kinds (object_picker, per_character_toggle,
scenario_freeform_text, prefab_holder) register through the SAME public
API from ``app/prefabs/builtin_kinds.py`` (imported by the package
``__init__``), so the staging routes never hardcode a kind. A new
custom kind only needs a ``data/prefabs/<id>/prefab.py`` that calls
``register_kind`` — no engine edits.

importlib note: prefab code lives under ``data/``, outside any package
root, so we load it via ``spec_from_file_location`` under a synthetic
``app.prefabs.loaded.<id>`` name, mirroring the module loader.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any

from flask import current_app


_LOADED: dict[str, Any] = {}


def _prefabs_dir() -> str:
    return os.path.join(current_app.config["data_dir"], "prefabs")


def _prefab_python_path(prefab_id: str) -> str:
    return os.path.join(_prefabs_dir(), prefab_id, "prefab.py")


def has_python(prefab_id: str) -> bool:
    """True iff ``data/prefabs/<id>/prefab.py`` exists."""
    try:
        return os.path.isfile(_prefab_python_path(prefab_id))
    except Exception:
        return False


def import_prefab(prefab_id: str) -> Any | None:
    """Import a prefab's Python entry, registering its kind handler(s)
    as side-effects. Errors are logged, never raised — a broken prefab
    shouldn't take down the engine."""
    path = _prefab_python_path(prefab_id)
    if not os.path.isfile(path):
        return None

    pkg_name = f"app.prefabs.loaded.{prefab_id}"
    try:
        spec = importlib.util.spec_from_file_location(pkg_name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[pkg_name] = mod
        spec.loader.exec_module(mod)
    except Exception as e:
        try:
            current_app.logger.warning(
                "Failed to import prefab %r: %s", prefab_id, e,
            )
        except Exception:
            pass
        sys.modules.pop(pkg_name, None)
        return None

    _LOADED[prefab_id] = mod
    return mod


def load_all_prefab_code() -> dict[str, Any]:
    """Discover every prefab manifest and import its Python entry, if
    present. Called once at app startup (after the builtin kinds are
    registered via the package import). Manifest-only prefabs (the four
    builtins, whose handlers live in ``builtin_kinds``) are simply
    absent from the return — that's a valid drop-in shape."""
    from . import all_manifests
    out: dict[str, Any] = {}
    for prefab_id in all_manifests().keys():
        if not has_python(prefab_id):
            continue
        mod = import_prefab(prefab_id)
        if mod is not None:
            out[prefab_id] = mod
    return out


def loaded() -> dict[str, Any]:
    return dict(_LOADED)
