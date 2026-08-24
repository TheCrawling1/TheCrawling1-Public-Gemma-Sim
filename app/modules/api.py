"""Public hooks API for module backend code.

Modules that ship Python under ``data/modules/<id>/<id>.py`` import
from this surface — never from engine internals. The contract:

  - ``is_active(conversation, leaf_id=None)`` — boolean: is the
    module on for the active path?
  - ``settings_for(conversation, leaf_id=None)`` — dict of the
    branch's per-module settings.
  - ``module_static_url(module_id, filename)`` — server-side URL
    builder for a module asset (mirrors the JS-side helper).
  - ``MODULE_ID`` — convention: each module's Python file declares
    ``MODULE_ID = "<id>"`` at the top so its helpers don't have to
    repeat the id literal.

Prompt-block registration imports from ``app.prompt`` directly:

    from app.prompt import register, register_filter, Block

That's the only "engine API" surface modules touch — everything
else flows through this file.
"""
from __future__ import annotations

from typing import Any

from flask import url_for

from ..effective import active_setup_root_for_path


def is_active(
    conversation: dict[str, Any],
    module_id: str,
    leaf_id: str | None = None,
) -> bool:
    """Return True if ``module_id`` is in the active setup root's
    ``modules`` list for the given path.

    Module activation is per-branch via the setup-root's metadata.
    No active setup root (legacy / pre-staging conversations) means
    no modules.
    """
    root = active_setup_root_for_path(conversation, leaf_id)
    if not root:
        return False
    mods = (root.get("metadata") or {}).get("modules") or []
    return isinstance(mods, list) and module_id in mods


def settings_for(
    conversation: dict[str, Any],
    module_id: str,
    leaf_id: str | None = None,
) -> dict[str, Any]:
    """Return the merged module settings for the active branch.

    Looks up the active setup root's ``metadata.module_settings.<id>``
    block. Defaults to empty dict when the module isn't on or hasn't
    declared any settings on the branch.
    """
    root = active_setup_root_for_path(conversation, leaf_id)
    if not root:
        return {}
    meta = root.get("metadata") or {}
    raw = (meta.get("module_settings") or {}).get(module_id) or {}
    return raw if isinstance(raw, dict) else {}


def module_static_url(module_id: str, filename: str) -> str:
    """Build the server URL for a module asset file.

    Maps to the ``/modules/<id>/static/<filename>`` route declared
    in ``app/routes/modules.py``. Path-scoped: filenames containing
    ``..`` or starting with ``/`` are rejected by the route handler,
    so this helper doesn't need to sanitize.
    """
    return url_for(
        "modules.static_asset",
        module_id=module_id,
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Engine facade
# ---------------------------------------------------------------------------
# The single sanctioned surface for the engine helpers gameplay modules need.
# Modules import these from HERE, not from the engine's internal modules
# directly, so the module <-> engine boundary is one stable seam: the engine
# can refactor its internals as long as these names keep resolving. (Before
# this, pf1e / life_sim reached into app.effective / app.entities /
# app.conversations / app.layers / app.mapnav / app.ollama_client / app.auth
# directly.)
#
# Two shapes are re-exported: individual FUNCTIONS (the preferred surface) and,
# as a pragmatic bridge for call sites that use `module.attr` access, a few
# whole engine MODULES. Narrowing the module re-exports to functions is a
# follow-up; the immediate win is that every module import now names one place.
from ..effective import (  # noqa: E402,F401
    effective_entities_at,
    effective_cast_at,
    effective_user_persona,
    # active_setup_root_for_path is already imported above for internal use.
)
from ..ollama_client import chat_sync  # noqa: E402,F401
from ..auth import login_required  # noqa: E402,F401
from ..layers import (  # noqa: E402,F401
    add_created_entity_to_cast,
    remove_from_conversation_cast,
)
from ..mapnav import locked_exits  # noqa: E402,F401

# Whole-module re-exports (bridge for `convs.load_conversation`,
# `_ent.load_instance_entity`, `layers.add_...`, `mapnav.locked_exits` style
# access). Importing a submodule attribute here binds it on this module so
# `from app.modules.api import conversations as convs` resolves.
from .. import conversations  # noqa: E402,F401
from .. import entities  # noqa: E402,F401
from .. import layers  # noqa: E402,F401
from .. import mapnav  # noqa: E402,F401
from .. import relationships  # noqa: E402,F401
from .. import memory  # noqa: E402,F401
from .. import rbd  # noqa: E402,F401
from .. import unspeakable  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Return by Death dispatch — the one-line seam a gameplay module's death path
# (pf1e, or any future combat module) calls when an actor dies. Keeps the
# activation policy ("only when the return_by_death module is on, and only the
# PLAYER's death rewinds") out of the calling module: pf1e just reports the
# death and this decides whether a loop resets.
# ---------------------------------------------------------------------------


def return_on_death(
    conversation: dict[str, Any],
    *,
    actor_id: str | None = "user",
    narrator_text: str | None = None,
    leaf_id: str | None = None,
) -> dict[str, Any] | None:
    """Death → Return by Death, for a combat/rules module to call on a kill.

    No-op (returns None) unless BOTH hold: the ``return_by_death`` module is
    active for the branch, and the dead actor is the player (``actor_id`` is
    ``"user"`` or ``None``). An NPC's death never rewinds the world. When it
    fires, spawns the fresh loop and returns the new loop-root message.

    This is the whole pf1e→RbD contract: pf1e reports *who* died, RbD owns the
    policy of *whether* that resets a loop — so the two stay decoupled and RbD
    can be dropped into any scenario without a rules module knowing it exists.
    """
    if actor_id not in (None, "user"):
        return None
    if not is_active(conversation, "return_by_death", leaf_id):
        return None
    new_root = rbd.return_by_death(conversation, narrator_text=narrator_text)
    if new_root is not None:
        _maybe_seal_player(conversation, new_root["id"])
    return new_root


def _maybe_seal_player(conversation: dict[str, Any], loop_root_id: str) -> None:
    """After a rewind, place the player under the Witch's constraint on the new
    loop-root — they now carry a loop they cannot speak of — when the
    ``return_by_death`` module has ``witch_constraint`` on (the default). Sealing
    is idempotent across loops, and branch-local, so a rewind to before any death
    (loop 1) leaves the player unsealed (they don't know the loop yet)."""
    if settings_for(conversation, "return_by_death").get("witch_constraint") is False:
        return
    unspeakable.seal(conversation, "user", leaf_id=loop_root_id)
