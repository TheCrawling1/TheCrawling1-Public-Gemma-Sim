"""Engine-provided builtin prefab kinds.

The four kinds that shipped with the prefab system — object_picker,
per_character_toggle, scenario_freeform_text, prefab_holder — registered
through the public ``register_kind`` API exactly the way a data/ drop-in
would. The staging routes dispatch every kind (builtin or drop-in)
through the registry; nothing in the route layer names a kind anymore.

Imported for its side effects by ``app/prefabs/__init__.py`` so the
builtins are live as soon as the package is imported.
"""
from __future__ import annotations

from typing import Any

from .api import (
    BaseKind,
    PrefabContext,
    composes_of,
    deep_merge,
    get,
    load_instance_or_template,
    object_catalog,
    read_prefab_data,
    register_kind,
    staging_kind_of,
    staging_ui_of,
    substitute_template,
)


def _object_brief(ent: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ent.get("id"),
        "name": ent.get("name") or ent.get("id"),
        "description": (ent.get("description") or "").strip(),
        "slot": ((ent.get("properties") or {}).get("slot") or ""),
    }


class ObjectPickerKind(BaseKind):
    """`objects` (scenario pool) and `generic_objects` (global catalog).
    Picks land as ``cast_add`` for free items, or as a per-owner
    ``properties.equipped`` patch for equipped items."""

    def build_panel(self, manifest, ui, cfg, ctx: PrefabContext):
        pool_source = ui.get("pool_source") or "scenario_declared"
        exclude_tags = set(cfg.get("exclude_tags") or [])
        resolved: list[dict[str, Any]] = []
        if pool_source == "global_catalog":
            for ent in ctx.catalog.values():
                if not isinstance(ent, dict) or ent.get("type") != "object":
                    continue
                if exclude_tags and any(t in exclude_tags for t in (ent.get("tags") or [])):
                    continue
                if not isinstance(ent.get("id"), str) or not ent.get("id"):
                    continue
                resolved.append(_object_brief(ent))
            resolved.sort(key=lambda r: (r["name"] or "").lower())
        else:
            for oid in (cfg.get("pool") or []):
                if not isinstance(oid, str):
                    continue
                ent = ctx.catalog.get(oid)
                if not ent or ent.get("type") != "object":
                    continue
                resolved.append(_object_brief(ent))
        return {"pool": resolved}

    def apply_picks(self, manifest, ui, cfg, pf_payload, ctx: PrefabContext):
        prefab_id = manifest.get("id")
        raw_objects = pf_payload.get("objects")
        raw_equipped = pf_payload.get("equipped")
        # Legacy top-level payload shape — only the `objects` prefab ever
        # lived there, so only consult it for that id.
        if not isinstance(raw_objects, list) and prefab_id == "objects":
            raw_objects = ctx.payload.get("objects")
        if not isinstance(raw_equipped, dict) and prefab_id == "objects":
            raw_equipped = ctx.payload.get("equipped")
        if not isinstance(raw_objects, list):
            raw_objects = []
        if not isinstance(raw_equipped, dict):
            raw_equipped = {}

        pool_source = ui.get("pool_source") or "scenario_declared"
        exclude_tags = set(cfg.get("exclude_tags") or [])
        valid_objects: list[str] = []
        if pool_source == "global_catalog":
            catalog = object_catalog()
            for oid in raw_objects:
                if not isinstance(oid, str) or oid not in catalog:
                    continue
                if exclude_tags and any(t in exclude_tags for t in (catalog[oid].get("tags") or [])):
                    continue
                valid_objects.append(oid)
        else:
            pool_ids = [o for o in (cfg.get("pool") or []) if isinstance(o, str)]
            valid_objects = [o for o in raw_objects if isinstance(o, str) and o in pool_ids]

        edits: list[dict[str, Any]] = []
        objects_by_owner: dict[str, list[str]] = {}
        equip_back: dict[str, str] = {}
        for oid in valid_objects:
            target = raw_equipped.get(oid)
            if isinstance(target, str) and target and target in ctx.picked_chars:
                objects_by_owner.setdefault(target, []).append(oid)
                equip_back[oid] = target
        for oid in valid_objects:
            if oid not in equip_back:
                edits.append({"kind": "cast_add", "id": oid})
        for char_id, owned in objects_by_owner.items():
            edits.append({
                "kind": "patch", "id": char_id,
                "data": {"properties": {"equipped": list(owned)}},
            })
        return edits


class PerCharacterToggleKind(BaseKind):
    """`futa` and friends — a chip per picked character. Toggling ON
    emits the manifest's ``on_toggle.edits`` for that character, with
    entity-side ``prefab_data`` deep-merged over the template, cascading
    through any composed per_character_toggle children."""

    def build_panel(self, manifest, ui, cfg, ctx: PrefabContext):
        pid = manifest.get("id")
        return {"ui": {
            "label": ui.get("label") or pid,
            "tooltip": ui.get("tooltip") or "",
            "default_off": bool(ui.get("default_off", True)),
            "include_user": bool(ui.get("include_user", False)),
        }}

    def apply_picks(self, manifest, ui, cfg, pf_payload, ctx: PrefabContext):
        prefab_id = manifest.get("id")
        raw_chars = pf_payload.get("characters")
        if not isinstance(raw_chars, list):
            return []
        chars = [c for c in raw_chars if isinstance(c, str) and c in ctx.picked_chars]
        if not chars:
            return []

        # Resolve the cascade chain (self + composed per_character_toggle
        # children), breadth-first, cycle-safe.
        chain: list[tuple[str, dict]] = []
        seen: set[str] = set()
        queue: list[str] = [prefab_id]
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            m = get(cur)
            if not m or staging_kind_of(m) != "per_character_toggle":
                continue
            chain.append((cur, staging_ui_of(m)))
            queue.extend(composes_of(m))

        edits: list[dict[str, Any]] = []
        picks_outfits = ctx.picks.get("outfits") or {}
        for char_id in chars:
            char_ent = load_instance_or_template(ctx.cid, char_id) or {}
            outfit_id = picks_outfits.get(char_id) if isinstance(picks_outfits, dict) else None
            if not (isinstance(outfit_id, str) and outfit_id):
                outfit_id = (char_ent.get("properties") or {}).get("current_outfit") or ""
            outfit_id = outfit_id or ""
            outfit_ent = load_instance_or_template(ctx.cid, outfit_id) if outfit_id else None
            subs = {"character_id": char_id, "current_outfit_id": outfit_id}

            for chain_pid, chain_ui in chain:
                chain_edits = (chain_ui.get("on_toggle") or {}).get("edits")
                if not isinstance(chain_edits, list):
                    continue
                char_pd = read_prefab_data(char_ent, chain_pid)
                outfit_pd = read_prefab_data(outfit_ent, chain_pid)
                for edit in substitute_template(chain_edits, subs):
                    if not isinstance(edit, dict) or not isinstance(edit.get("kind"), str):
                        continue
                    merge_char = bool(edit.pop("merge_character_prefab_data", False))
                    merge_outfit = bool(edit.pop("merge_current_outfit_prefab_data", False))
                    if merge_char or merge_outfit:
                        base = edit.get("data")
                        if not isinstance(base, dict):
                            base = {}
                        if merge_char and isinstance(char_pd, dict):
                            deep_merge(base, char_pd)
                        if merge_outfit and isinstance(outfit_pd, dict):
                            deep_merge(base, outfit_pd)
                        edit["data"] = base
                    edits.append(edit)
        return edits


class ScenarioFreeformTextKind(BaseKind):
    """`scene_effects` — author's-note text. ``applies_to`` modes:

      - ``all_picked_characters`` (default): one shared note patched onto
        every picked character (full-cast rule).
      - ``per_character``: a separate note per character, read from
        ``pf_payload.texts = {char_id: text}`` — only that character is
        patched. Fixes the full-cast-only limitation.
      - ``shared_and_per_character``: a cast-wide note (``pf_payload.text``)
        plus optional per-character notes (``pf_payload.texts``). Each
        picked character gets the shared note with its own note appended
        below it; characters with neither are skipped.
    """

    def build_panel(self, manifest, ui, cfg, ctx: PrefabContext):
        pid = manifest.get("id")
        return {"ui": {
            "label": ui.get("label") or pid,
            "placeholder": ui.get("placeholder") or "",
            "rows": int(ui.get("rows", 4)),
            "default_text": ui.get("default_text") or "",
            "applies_to": ui.get("applies_to") or "all_picked_characters",
            "per_character_label": ui.get("per_character_label") or "Per-character notes",
            "per_character_placeholder": ui.get("per_character_placeholder") or "",
        }}

    def apply_picks(self, manifest, ui, cfg, pf_payload, ctx: PrefabContext):
        prefab_id = manifest.get("id")
        field_key = ui.get("character_field") or prefab_id
        label = ui.get("label") or prefab_id
        applies_to = ui.get("applies_to") or "all_picked_characters"
        edits: list[dict[str, Any]] = []

        def _patch(char_id: str, text: str) -> dict[str, Any]:
            return {
                "kind": "patch", "id": char_id,
                "data": {"properties": {field_key: {"label": label, "text": text}}},
            }

        if applies_to == "per_character":
            texts = pf_payload.get("texts")
            if not isinstance(texts, dict):
                return []
            for char_id, text in texts.items():
                if char_id not in ctx.picked_chars:
                    continue
                if not isinstance(text, str) or not text.strip():
                    continue
                edits.append(_patch(char_id, text.strip()))
            return edits

        if applies_to == "shared_and_per_character":
            shared = pf_payload.get("text")
            shared = shared.strip() if isinstance(shared, str) else ""
            texts = pf_payload.get("texts")
            per = texts if isinstance(texts, dict) else {}
            for char_id in ctx.picked_chars:
                own = per.get(char_id)
                own = own.strip() if isinstance(own, str) and own.strip() else ""
                combined = "\n\n".join(t for t in (shared, own) if t)
                if combined:
                    edits.append(_patch(char_id, combined))
            return edits

        raw_text = pf_payload.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            return []
        for char_id in ctx.picked_chars:
            edits.append(_patch(char_id, raw_text.strip()))
        return edits


class PrefabHolderKind(BaseKind):
    """`prefab_holder` — a UI container only. Emits no edits; its
    composed children are surfaced/fired through their own kinds."""
    pass


register_kind("object_picker", ObjectPickerKind())
register_kind("per_character_toggle", PerCharacterToggleKind())
register_kind("scenario_freeform_text", ScenarioFreeformTextKind())
register_kind("prefab_holder", PrefabHolderKind())
