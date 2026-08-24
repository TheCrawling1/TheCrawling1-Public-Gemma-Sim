"""Auto-discover and import module backend code.

Each module under ``data/modules/<id>/`` may ship an ``<id>.py`` file
alongside its ``module.json`` manifest. When present, that file is
imported at engine startup — its top-level statements run (prompt-
block registrations, filter registrations, blueprint mounts) and the
module's contributions become live.

Loading happens once, during ``load_all_module_code()``, which the
app factory calls after the Flask app is configured. Modules
discovered later (via ``reload()``) are re-imported on the next
``load_all_module_code()`` call.

Modules import from the stable public surface (``app.prompt`` and
``app.modules.api``) — never from engine internals. The engine never
imports module code by name. This keeps modules drop-in: adding /
removing a module folder is the only step needed.

Why importlib (not the standard import machinery): module code lives
under ``data/``, not under any importable package root. We construct
the import via ``importlib.util.spec_from_file_location`` so each
module file is loaded with a stable ``app.modules.loaded.<id>`` name
without needing ``__init__.py`` files in data/.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any

from flask import current_app


_LOADED: dict[str, Any] = {}


def _modules_dir() -> str:
    return os.path.join(current_app.config["data_dir"], "modules")


def _module_python_path(module_id: str) -> str:
    """Return the absolute filesystem path to the module's Python entry."""
    return os.path.join(_modules_dir(), module_id, f"{module_id}.py")


def has_python(module_id: str) -> bool:
    """True iff `data/modules/<id>/<id>.py` exists."""
    try:
        return os.path.isfile(_module_python_path(module_id))
    except Exception:
        return False


def import_module(module_id: str) -> Any | None:
    """Import the module's Python entry, registering its prompt blocks
    / filters / blueprints as side-effects. Idempotent: re-importing
    an already-loaded module re-runs its top-level statements (useful
    for hot-reload during development).

    Returns the imported Python module object, or None when the file
    is missing or fails to load. Errors are logged via the Flask app
    logger but never raised — a broken module shouldn't take down the
    whole engine.
    """
    path = _module_python_path(module_id)
    if not os.path.isfile(path):
        return None

    pkg_name = f"app.modules.loaded.{module_id}"
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
                "Failed to import module %r: %s", module_id, e,
            )
        except Exception:
            pass
        sys.modules.pop(pkg_name, None)
        return None

    _LOADED[module_id] = mod
    return mod


def load_all_module_code() -> dict[str, Any]:
    """Discover every module manifest and import its Python entry.

    Called once during app startup (see `app.__init__`). Returns the
    map of `{module_id: imported_module}` for the modules that loaded
    cleanly. Modules without an `<id>.py` are simply absent from the
    return — those are manifest-only modules (e.g. autoplay before
    its Phase 3 refactor), which is a valid drop-in shape for JS-only
    or settings-only modules.
    """
    from . import all_manifests
    out: dict[str, Any] = {}
    for module_id in all_manifests().keys():
        if not has_python(module_id):
            continue
        mod = import_module(module_id)
        if mod is not None:
            out[module_id] = mod
    return out


def loaded() -> dict[str, Any]:
    """Return the {module_id: imported_module} table of currently-loaded
    backends. Refreshes implicitly only when `import_module` runs."""
    return dict(_LOADED)
