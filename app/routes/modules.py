"""Module asset serving.

Modules ship their own JS / CSS / image files under ``data/modules/
<id>/``. The chat page loads them via ``<script src="/modules/<id>/
static/<filename>">`` (and ``<link href=...>`` for CSS). This route
serves each module's ``static/`` subdir under a path-scoped namespace
so two modules can't reach into each other and a request can't escape
out of ``data/modules/<id>/``.

Layout convention:

    data/modules/<id>/
      module.json
      <id>.py        # backend (optional)
      <id>.js        # frontend (optional, auto-loaded when active)
      <id>.css       # styles (optional, auto-loaded when active)
      static/        # additional assets the module's JS / CSS uses
        icon.svg
        bubble.png

By convention the auto-loaded ``<id>.js`` and ``<id>.css`` live at
the top of the module dir, but they're served via the same route
(``/modules/<id>/static/<id>.js`` resolves to ``data/modules/<id>/
<id>.js``). This keeps the URL shape consistent regardless of where
the file actually sits.
"""
from __future__ import annotations

import os

from flask import Blueprint, abort, current_app, send_from_directory

from ..auth import login_required


bp = Blueprint("modules", __name__, url_prefix="/modules")


def _module_dir(module_id: str) -> str:
    """Absolute path to the module's directory. Validates the id
    against the manifest table to keep a stray request from probing
    arbitrary subdirectories of ``data/modules``."""
    from .. import modules as _modules_mod
    if module_id not in _modules_mod.all_manifests():
        return ""
    return os.path.join(
        current_app.config["data_dir"], "modules", module_id,
    )


@bp.get("/<module_id>/static/<path:filename>")
@login_required
def static_asset(module_id: str, filename: str):
    """Serve `<module_dir>/<filename>` for a registered module.

    Looks both at the module's top-level file (e.g. ``texting.js``)
    and at the ``static/`` subdir for additional assets. Top-level
    file takes precedence — that's where the auto-loaded ``<id>.js``
    / ``<id>.css`` live.

    Path safety: ``send_from_directory`` rejects ``..`` segments and
    absolute paths. The module-id validation also blocks requests
    that target a non-manifested id.
    """
    root = _module_dir(module_id)
    if not root:
        abort(404)
    # Try the module root first (where the auto-loaded files live),
    # then fall back to the static/ subdir for additional assets.
    top_level_path = os.path.join(root, filename)
    if os.path.isfile(top_level_path):
        return send_from_directory(root, filename)
    static_dir = os.path.join(root, "static")
    if os.path.isdir(static_dir):
        candidate = os.path.join(static_dir, filename)
        if os.path.isfile(candidate):
            return send_from_directory(static_dir, filename)
    abort(404)
