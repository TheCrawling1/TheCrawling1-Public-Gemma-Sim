"""Prefab asset serving.

Drop-in prefabs may ship a frontend renderer + styles under
``data/prefabs/<id>/`` (``prefab.js`` / ``prefab.css``, plus a
``static/`` subdir for extra assets). The chat page loads them via
``<script src="/prefabs/<id>/static/prefab.js">`` (and ``<link>`` for
CSS). This route serves each prefab's files under a path-scoped
namespace so a request can't escape ``data/prefabs/<id>/``.

Mirrors ``app/routes/modules.py`` — same shape, same safety.
"""
from __future__ import annotations

import os

from flask import Blueprint, abort, current_app, send_from_directory

from ..auth import login_required


bp = Blueprint("prefabs", __name__, url_prefix="/prefabs")


def _prefab_dir(prefab_id: str) -> str:
    """Absolute path to the prefab's directory, gated on the id being a
    registered manifest so a stray request can't probe arbitrary dirs."""
    from .. import prefabs as _prefabs_mod
    if prefab_id not in _prefabs_mod.all_manifests():
        return ""
    return os.path.join(current_app.config["data_dir"], "prefabs", prefab_id)


@bp.get("/<prefab_id>/static/<path:filename>")
@login_required
def static_asset(prefab_id: str, filename: str):
    """Serve ``<prefab_dir>/<filename>`` for a registered prefab.

    Top-level file (where the auto-loaded ``prefab.js`` / ``prefab.css``
    live) takes precedence, then the ``static/`` subdir. Path safety is
    handled by ``send_from_directory`` plus the manifest-id gate.
    """
    root = _prefab_dir(prefab_id)
    if not root:
        abort(404)
    top_level_path = os.path.join(root, filename)
    if os.path.isfile(top_level_path):
        return send_from_directory(root, filename)
    static_dir = os.path.join(root, "static")
    if os.path.isdir(static_dir):
        candidate = os.path.join(static_dir, filename)
        if os.path.isfile(candidate):
            return send_from_directory(static_dir, filename)
    abort(404)
