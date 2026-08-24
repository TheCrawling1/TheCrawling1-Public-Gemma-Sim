"""Prefab staging-kind registry.

A *kind* (``staging_ui.kind``) is the behavior contract for a class of
prefab — how its staging-panel section is built and how its picks turn
into branch-root edits. Handlers register here keyed by kind name; the
staging routes dispatch through the registry instead of hardcoding an
``if kind == ...`` chain. This is what makes prefabs drop-in: a new kind
ships its handler via ``register_kind`` (engine-provided builtins use the
same public API as data/ drop-ins) and the engine never names it.

Mirrors ``app.prompt.registry`` for modules — same register/lookup shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PrefabContext:
    """Everything a kind handler needs from the staging routes.

    Panel-build uses ``catalog`` (the resolved entity catalog). Apply
    uses ``cid`` / ``picks`` / ``payload`` / ``picked_chars``. Both
    carry ``scenario`` so a handler can read sibling config. Fields not
    relevant to a given call are left at their defaults.
    """
    scenario: dict[str, Any]
    # --- apply-time ---
    cid: str | None = None
    picks: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    picked_chars: set[str] = field(default_factory=set)
    # --- panel-time ---
    catalog: dict[str, dict[str, Any]] = field(default_factory=dict)


class BaseKind:
    """Default no-op handler. Subclasses override one or both methods.

    ``build_panel`` returns a dict merged into the panel block the
    frontend renders. ``apply_picks`` returns a list of edits appended
    to the new branch root. ``pf_payload`` is the per-prefab pick state
    the client sent under ``payload.prefabs[<id>]``.
    """

    def build_panel(
        self,
        manifest: dict[str, Any],
        ui: dict[str, Any],
        cfg: dict[str, Any],
        ctx: PrefabContext,
    ) -> dict[str, Any]:
        return {}

    def apply_picks(
        self,
        manifest: dict[str, Any],
        ui: dict[str, Any],
        cfg: dict[str, Any],
        pf_payload: dict[str, Any],
        ctx: PrefabContext,
    ) -> list[dict[str, Any]]:
        return []


_KINDS: dict[str, BaseKind] = {}


def register_kind(kind: str, handler: BaseKind) -> None:
    """Register (or replace) the handler for a staging_ui kind.

    Idempotent-by-replacement: re-registering the same kind overwrites,
    which is what hot-reload during development wants.
    """
    if not isinstance(kind, str) or not kind:
        raise ValueError("kind must be a non-empty string")
    _KINDS[kind] = handler


def get_kind(kind: str) -> BaseKind | None:
    return _KINDS.get(kind)


def all_kinds() -> dict[str, BaseKind]:
    return dict(_KINDS)


def clear_kinds() -> None:
    """Drop all registrations (used by reload paths / tests)."""
    _KINDS.clear()
