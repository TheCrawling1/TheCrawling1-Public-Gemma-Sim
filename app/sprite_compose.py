"""Local sprite compositor.

Builds the chat image from local files instead of fetching it from a
remote sprite server. Inputs:

  - sprite_id   → directory under ``config.sprite_assets_dir`` holding
                  the layer PNGs.
  - slots       → 8-tuple of clothing-slot ints (top, bottom, bra,
                  underwear, pantyhose, gloves, legwear, shoes). 1 = on,
                  2 = partial, 3 = off.
  - garments    → 8-tuple of garment IDs per slot, naming which version
                  to render in that slot. ``None`` (or empty string) per
                  slot means "use the wardrobe's `default` garment for
                  this slot." Slots whose state is 3 contribute nothing
                  regardless of the garment id.
  - scene_tag   → name of a background PNG in ``<assets>/Scene/``.

Layer resolution has two modes:

  1. **Wardrobe mode** (preferred). If ``<assets>/<sprite_id>/wardrobe.json``
     exists, it's the per-character layer catalog: for each slot, an ID →
     state → relative-PNG-path map. Authors add new garments by dropping
     a PNG in the sprite dir, adding an entry to wardrobe.json, and
     referencing the new id from an outfit's ``garments`` map.

  2. **Legacy mode**. When wardrobe.json is missing, the compositor uses
     the original hard-coded 14-image catalog (``Image1.png`` …
     ``Image14.png``). Existing characters that haven't been migrated
     keep working unchanged. The ``garments`` argument is ignored in
     legacy mode.

Cache keys hash the resolved layer paths so a wardrobe edit (or a
garment swap) misses cache the way it should.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, Iterable

from flask import current_app
from PIL import Image


# ---------------------------------------------------------------------------
# Legacy catalog — used when no wardrobe.json is present
# ---------------------------------------------------------------------------

_BASE_CATALOG: dict[tuple[int, ...], int] = {
    (3, 3, 3, 3, 3, 3, 3, 3): 1,   # base body
    (2, 2, 3, 3, 3, 3, 3, 3): 2,   # top=partial + bottom=partial
    (2, 1, 3, 3, 3, 3, 3, 3): 3,   # top=partial + bottom=on
    (1, 3, 3, 3, 3, 3, 3, 3): 4,   # top alone (on)
    (1, 2, 3, 3, 3, 3, 3, 3): 5,   # top=on + bottom=partial
    (1, 1, 3, 3, 3, 3, 3, 3): 6,   # top=on + bottom=on (full outerwear)
    (3, 3, 3, 3, 3, 3, 1, 3): 7,   # legwear=on
    (3, 3, 3, 2, 3, 3, 3, 3): 8,   # underwear=partial
    (3, 3, 3, 1, 3, 3, 3, 3): 9,   # underwear=on
    (3, 3, 2, 3, 3, 3, 3, 3): 10,  # bra=partial
    (3, 3, 1, 3, 3, 3, 3, 3): 11,  # bra=on
    (3, 2, 3, 3, 3, 3, 3, 3): 12,  # bottom=partial alone
    (3, 1, 3, 3, 3, 3, 3, 3): 13,  # bottom=on alone
    (2, 3, 3, 3, 3, 3, 3, 3): 14,  # top=partial alone
}

# Layering order: lowest first, highest (most occluding) last. Underwear
# below outerwear so a uniform top covers the bra.
_LAYER_ORDER = ("base", "bra", "underwear", "legwear", "top_bottom", "bottom",
                "top", "pantyhose", "gloves", "shoes", "overlay")

# Slot index → wardrobe slot key, for single-slot layers. pantyhose,
# gloves and shoes render OVER the top/bottom garment (see _LAYER_ORDER) —
# e.g. a capelet filed in the pantyhose slot drapes over the dress. Legacy
# (non-wardrobe) characters are unaffected: their _BASE_CATALOG has no keys
# for these slots, so the "if key in _BASE_CATALOG" guards skip them.
_SINGLE_SLOT_KINDS: dict[int, str] = {
    2: "bra", 3: "underwear", 4: "pantyhose", 5: "gloves", 6: "legwear", 7: "shoes",
}

# Slot index → state name. Used to translate slots[i] into the wardrobe
# state key. (1=on, 2=partial; 3=off contributes nothing.)
_STATE_NAMES = {1: "on", 2: "partial"}

DEFAULT_GARMENT = "default"

# Canonical sprite slot name → index, mirroring ``sprite_url.SLOT_ORDER``.
# Used by the accessory policy to read an explicit required-slots list.
# Kept local (not imported) so this module has no import-time dependency on
# sprite_url; the two lists must stay in lockstep.
_SLOT_NAME_TO_INDEX: dict[str, int] = {
    "top": 0, "bottom": 1, "bra": 2, "underwear": 3,
    "pantyhose": 4, "gloves": 5, "legwear": 6, "shoes": 7,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def cache_key_for(
    sprite_id: str,
    slots: tuple[int, ...],
    scene_tag: str,
    garments: tuple[str, ...] | None = None,
    *,
    crop_solo: bool = False,
    transparency: tuple[int, ...] | None = None,
) -> str:
    """Return the deterministic cache key for a render. Used by the
    route layer to serve a strong ETag without actually composing the
    PNG."""
    layer_paths = _resolve_layer_paths(sprite_id, slots, garments, transparency)
    return _cache_key(
        sprite_id, slots, garments, scene_tag, layer_paths,
        crop_solo=crop_solo, transparency=transparency,
    )


def compose_png(
    sprite_id: str,
    slots: tuple[int, ...],
    scene_tag: str,
    garments: tuple[str, ...] | None = None,
    *,
    crop_solo: bool = False,
    transparency: tuple[int, ...] | None = None,
) -> bytes:
    """Return PNG bytes for the composed image.

    Looks up the sprite asset dir from
    ``current_app.config['sprite_assets_dir']`` (falling back to
    ``Temp characters/new image addon junk``). Cached on disk by content
    hash; subsequent calls with the same args read straight from the
    cache file.

    `crop_solo` returns the center third of the canvas (the solo route
    sets this so a single character is more visible against a wide
    16:9 scene). Group composes never crop.

    `transparency` is an 8-tuple of 0..100 ints aligned with
    ``SLOT_ORDER`` (top, bottom, bra, underwear, pantyhose, gloves,
    legwear, shoes); 100 = fully visible (default). Lower values
    multiply the alpha of any layer that fills that slot, so e.g.
    ``top=50`` makes a uniform shirt half see-through. ``None`` means
    "every slot at 100".

    Raises FileNotFoundError if the scene PNG or any layer file is
    missing — let the route turn that into a 404.
    """
    assets = _assets_dir()
    layer_paths = _resolve_layer_paths(sprite_id, slots, garments, transparency)
    scene_path = assets / "Scene" / f"{scene_tag}.png"
    if not scene_path.exists():
        raise FileNotFoundError(f"scene not found: {scene_path}")
    abs_layer_paths = [(kind, assets / sprite_id / rel) for kind, rel in layer_paths]
    for _, p in abs_layer_paths:
        if not p.exists():
            raise FileNotFoundError(f"sprite layer not found: {p}")

    cache_key = _cache_key(
        sprite_id, slots, garments, scene_tag, layer_paths,
        crop_solo=crop_solo, transparency=transparency,
    )
    cache_path = assets / ".cache" / f"{cache_key}.png"
    if cache_path.exists():
        return cache_path.read_bytes()

    composed = Image.open(scene_path).convert("RGBA")
    target_size = composed.size
    for kind, p in abs_layer_paths:
        layer = Image.open(p).convert("RGBA")
        if layer.size != target_size:
            # Backgrounds drive the canvas size; sprites scale to match.
            layer = layer.resize(target_size, Image.LANCZOS)
        layer = _apply_layer_transparency(layer, kind, transparency)
        composed = Image.alpha_composite(composed, layer)

    if crop_solo:
        composed = _crop_center_third(composed)

    buf = io.BytesIO()
    composed.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return data


def _crop_center_third(img: Image.Image) -> Image.Image:
    """Crop to the center third (horizontally), keeping full height.
    Used by solo composes — a 3840×2160 scene becomes 1280×2160 so the
    character fills more of the viewport."""
    w, h = img.size
    left = w // 3
    right = (w * 2) // 3
    return img.crop((left, 0, right, h))


def _apply_layer_transparency(
    layer: "Image.Image",
    kind: str,
    transparency: tuple[int, ...] | None,
) -> "Image.Image":
    """Multiply a layer's alpha channel by the per-slot transparency
    (0..100). Returns the layer untouched when transparency is None /
    100 / not applicable to the slot kind."""
    if transparency is None:
        return layer
    pcts = _layer_transparency_pcts(kind, transparency)
    if not pcts or all(p >= 100 for p in pcts):
        return layer
    pct = min(pcts)
    if pct <= 0:
        # Fully transparent — return an empty layer of the same size
        # so alpha_composite preserves underlying layers untouched.
        from PIL import Image as _Im
        return _Im.new("RGBA", layer.size, (0, 0, 0, 0))
    factor = pct / 100.0
    r, g, b, a = layer.split()
    a = a.point(lambda v: int(v * factor))
    from PIL import Image as _Im
    return _Im.merge("RGBA", (r, g, b, a))


_LAYER_KIND_TO_SLOT_INDICES: dict[str, tuple[int, ...]] = {
    # base body always renders fully — transparency-on-skin would
    # produce hollow figures, which isn't a useful effect to expose.
    "base": (),
    # overlay (base-body hand composited on top) — same: always solid.
    "overlay": (),
    "top": (0,),
    "bottom": (1,),
    # top_bottom is a single combined layer (e.g. one-piece dress);
    # the lowest of top/bottom transparency wins so authors can fade
    # the whole garment by setting either slot.
    "top_bottom": (0, 1),
    "bra": (2,),
    "underwear": (3,),
    "pantyhose": (4,),
    "gloves": (5,),
    "legwear": (6,),
    "shoes": (7,),
}


def _layer_transparency_pcts(
    kind: str, transparency: tuple[int, ...],
) -> list[int]:
    indices = _LAYER_KIND_TO_SLOT_INDICES.get(kind) or ()
    out: list[int] = []
    for idx in indices:
        if idx < len(transparency):
            try:
                out.append(int(transparency[idx]))
            except (TypeError, ValueError):
                out.append(100)
    return out


def compose_multi_png(
    participants: list[tuple[str, tuple[int, ...], tuple[str, ...] | None]],
    scene_tag: str,
    *,
    transparency_per: list[tuple[int, ...] | None] | None = None,
) -> bytes:
    """Return PNG bytes for a side-by-side multi-character composite.

    Each participant is a ``(sprite_id, slots, garments)`` tuple. Their
    layer stacks are alpha-composited onto a transparent canvas the
    same size as the scene background, then shifted horizontally so
    each character's canvas-center lands at the column-center of an
    N-column split. The shifted, transparent canvases are blended over
    a single shared scene PNG.

    Why shift instead of scale: VN sprites occupy the full canvas but
    the actual painted character takes up only the middle portion,
    with transparent margins on either side. Shifting sprite *i* by
    ``(i + 0.5) * W/N - W/2`` moves character *i* out of dead-center
    by exactly the right amount to land in column *i*; characters end
    up evenly spaced without scaling, and their transparent margins
    keep them from visually overlapping when sprites are positioned
    near canvas-center (which they are by author convention).

    Cached on disk like ``compose_png``; cache key hashes every
    participant's resolved layer paths plus the scene.

    Raises FileNotFoundError if the scene or any layer is missing.
    """
    assets = _assets_dir()
    if not participants:
        raise ValueError("compose_multi_png needs at least one participant")
    scene_path = assets / "Scene" / f"{scene_tag}.png"
    if not scene_path.exists():
        raise FileNotFoundError(f"scene not found: {scene_path}")

    # Resolve every participant's layer paths up front so the cache
    # key + the existence check happen before we touch PIL.
    resolved: list[tuple[str, list[tuple[str, str]]]] = []
    for i, (sprite_id, slots, garments) in enumerate(participants):
        tr = transparency_per[i] if transparency_per else None
        layers = _resolve_layer_paths(sprite_id, slots, garments, tr)
        for _, rel in layers:
            p = assets / sprite_id / rel
            if not p.exists():
                raise FileNotFoundError(f"sprite layer not found: {p}")
        resolved.append((sprite_id, layers))

    cache_key = _multi_cache_key(
        participants, scene_tag, resolved, transparency_per=transparency_per,
    )
    cache_path = assets / ".cache" / f"multi-{cache_key}.png"
    if cache_path.exists():
        return cache_path.read_bytes()

    scene = Image.open(scene_path).convert("RGBA")
    canvas_w, canvas_h = scene.size
    n = len(participants)
    composed = scene.copy()

    for i, (sprite_id, layers) in enumerate(resolved):
        # Build this character's stack on a transparent canvas.
        char = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        tr = (transparency_per[i] if transparency_per else None)
        for kind, rel in layers:
            layer = Image.open(assets / sprite_id / rel).convert("RGBA")
            if layer.size != (canvas_w, canvas_h):
                layer = layer.resize((canvas_w, canvas_h), Image.LANCZOS)
            layer = _apply_layer_transparency(layer, kind, tr)
            char = Image.alpha_composite(char, layer)

        # Horizontal shift so canvas-center → column-center.
        x_offset = int((i + 0.5) * canvas_w / n - canvas_w / 2)
        if x_offset != 0:
            shifted = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
            shifted.paste(char, (x_offset, 0), char)
            char = shifted
        composed = Image.alpha_composite(composed, char)

    buf = io.BytesIO()
    composed.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return data


def cache_key_for_multi(
    participants: list[tuple[str, tuple[int, ...], tuple[str, ...] | None]],
    scene_tag: str,
    *,
    transparency_per: list[tuple[int, ...] | None] | None = None,
) -> str:
    """Multi-character cache key for ETag / 304 handling at the route.
    Resolves layer paths the same way ``compose_multi_png`` does so a
    wardrobe edit invalidates the key the way it should.
    """
    resolved: list[tuple[str, list[tuple[str, str]]]] = []
    for i, (sprite_id, slots, garments) in enumerate(participants):
        tr = transparency_per[i] if transparency_per else None
        layers = _resolve_layer_paths(sprite_id, slots, garments, tr)
        resolved.append((sprite_id, layers))
    return _multi_cache_key(
        participants, scene_tag, resolved, transparency_per=transparency_per,
    )


def parse_slots(csv: str) -> tuple[int, ...]:
    """Parse the comma-CSV slot string into an 8-tuple of ints clamped to
    the legal range. Padded/truncated to length 8."""
    parts = [p.strip() for p in (csv or "").split(",")]
    out: list[int] = []
    for p in parts[:8]:
        try:
            n = int(p)
        except ValueError:
            n = 1
        if n not in (1, 2, 3):
            n = 1
        out.append(n)
    while len(out) < 8:
        out.append(1)
    return tuple(out)


def parse_transparency(csv: str) -> tuple[int, ...]:
    """Parse the comma-CSV transparency string into an 8-tuple of ints
    in [0, 100]. ``-`` and empty entries default to 100 (fully visible).
    Out-of-range values are clamped. Padded/truncated to length 8."""
    parts = [p.strip() for p in (csv or "").split(",")]
    out: list[int] = []
    for p in parts[:8]:
        if p in ("", "-"):
            out.append(100)
            continue
        try:
            n = int(p)
        except ValueError:
            n = 100
        if n < 0:
            n = 0
        elif n > 100:
            n = 100
        out.append(n)
    while len(out) < 8:
        out.append(100)
    return tuple(out)


def transparency_csv(transparency: dict[str, Any] | None) -> str:
    """Serialise a per-slot transparency dict (slot-name → 0..100) into
    the wire-format CSV in canonical slot order. Unset / 100 slots
    become ``-``. Returns ``""`` when nothing is below 100 — callers
    can use that to skip the optional segment entirely."""
    from .sprite_url import SLOT_ORDER  # local import avoids cycle
    if not isinstance(transparency, dict) or not transparency:
        return ""
    parts: list[str] = []
    any_set = False
    for name in SLOT_ORDER:
        raw = transparency.get(name)
        if raw is None:
            for k, v in transparency.items():
                if isinstance(k, str) and k.lower() == name:
                    raw = v
                    break
        if raw is None:
            parts.append("-")
            continue
        try:
            n = int(raw)
        except (TypeError, ValueError):
            parts.append("-")
            continue
        if n < 0:
            n = 0
        elif n > 100:
            n = 100
        if n >= 100:
            parts.append("-")
        else:
            parts.append(str(n))
            any_set = True
    return ",".join(parts) if any_set else ""


def parse_garments(csv: str) -> tuple[str, ...]:
    """Parse the comma-CSV garment string into an 8-tuple of garment ids.

    Empty strings and the literal ``-`` are normalized to ``""`` (meaning
    "use the wardrobe default"). Padded/truncated to length 8.
    """
    parts = [p.strip() for p in (csv or "").split(",")]
    out: list[str] = []
    for p in parts[:8]:
        if p in ("", "-"):
            out.append("")
        else:
            # Reject path separators / dot-segments so authors can't
            # tunnel out of the sprite dir via a garment id.
            safe = p.replace("..", "").replace("/", "").replace("\\", "")
            out.append(safe)
    while len(out) < 8:
        out.append("")
    return tuple(out)


def garments_csv(garments: dict[str, Any] | None) -> str:
    """Serialise an outfit-style ``garments`` dict (slot-name → id) into
    the wire-format CSV in canonical slot order. Empty / missing slots
    become ``-``. Returns an empty string when the dict is None or every
    slot is default — callers can use that to skip the optional segment
    entirely.
    """
    from .sprite_url import SLOT_ORDER  # local import avoids cycle
    if not isinstance(garments, dict) or not garments:
        return ""
    parts: list[str] = []
    any_set = False
    for name in SLOT_ORDER:
        raw = garments.get(name)
        if raw is None:
            for k, v in garments.items():
                if isinstance(k, str) and k.lower() == name:
                    raw = v
                    break
        if isinstance(raw, str) and raw.strip() and raw.strip() != "-":
            parts.append(raw.strip())
            any_set = True
        else:
            parts.append("-")
    return ",".join(parts) if any_set else ""


# ---------------------------------------------------------------------------
# Internals — resolution
# ---------------------------------------------------------------------------


def _resolve_layer_paths(
    sprite_id: str,
    slots: tuple[int, ...],
    garments: tuple[str, ...] | None,
    transparency: tuple[int, ...] | None = None,
) -> list[tuple[str, str]]:
    """Return the ordered list of ``(kind, relative_path)`` tuples to
    composite, top-down. ``kind`` is the wardrobe-slot label
    (``base``, ``top``, ``bottom``, ``top_bottom``, ``bra``,
    ``underwear``, ``legwear``) so the compositor can map each layer
    back to a slot for per-layer effects (e.g. transparency).

    Wardrobe mode if ``<sprite_id>/wardrobe.json`` exists, else legacy.
    """
    assets = _assets_dir()
    s = tuple(int(x) for x in slots)
    if len(s) != 8:
        raise ValueError(f"expected 8 slots, got {len(s)}")
    if garments is None:
        g: tuple[str, ...] = ("",) * 8
    else:
        g = tuple(garments)
        if len(g) != 8:
            raise ValueError(f"expected 8 garments, got {len(g)}")

    wardrobe = _load_wardrobe(assets, sprite_id)
    if wardrobe is not None:
        return _wardrobe_layers(s, g, wardrobe, transparency)
    return _legacy_layers(s)


def _policy_gate_indices(rule: Any, fallback: list[int]) -> list[int]:
    """Slot indices an ``all_on`` rule gates on. Prefers an explicit
    ``slots`` list of canonical slot names (so a fully-off slot still
    counts); otherwise falls back to the slots currently carrying the
    garment. Unknown slot names are ignored."""
    if isinstance(rule, dict):
        names = rule.get("slots")
        if isinstance(names, (list, tuple)):
            gate = [
                _SLOT_NAME_TO_INDEX[n]
                for n in names
                if isinstance(n, str) and n in _SLOT_NAME_TO_INDEX
            ]
            if gate:
                return gate
    return fallback


def _apply_accessory_policy(
    slots: tuple[int, ...],
    garments: tuple[str, ...],
    wardrobe: dict[str, Any],
) -> tuple[str, ...]:
    """Swap a garment to its plain (no-accessory) variant when the current
    clothing state means the accessories shouldn't show, and return the
    rewritten garments tuple. The base pick + every slot lookup then use
    the resolved variant uniformly.

    Some outfits bake their accessories (plating, crest pin, choker,
    ribbons) into every captured layer because the capture system couldn't
    isolate them; the wardrobe ships a plain garment variant alongside the
    accessory one and declares, per garment, when each applies::

        "accessory_policy": {
          "<garment_id>": {"mode": "all_on"|"worn"|"always", "plain": "<garment_id>"}
        }

    (A bare string value is shorthand for ``{"mode": <string>}``.)

      - ``all_on``  — accessories only when EVERY slot wearing this garment
                      is fully on (state 1); any half (2) or off (3) swaps
                      to ``plain``. (Serena's armor dress.)
      - ``always``  — accessories stay while the garment is worn on any
                      slot, even stripped to underwear. (Serena's maid.)
      - ``worn``    — default: accessories drop only when the garment is
                      off on every slot it occupies (unclothed).

    ``all_on`` may name the exact slots to gate on via a ``slots`` list of
    canonical slot names::

        "armor": {"mode": "all_on", "plain": "armorplain",
                  "slots": ["top", "bottom", "gloves", "legwear", "shoes"]}

    This matters because a fully-*off* slot drops its garment id (the v2
    resolver reports ``None`` for an unworn slot), so the garment-derived
    slot set can't tell "bottom taken off" from "outfit never had a bottom".
    Listing the slots explicitly lets a single removed piece drop the
    accessories, honouring "any part half **or off**". Without ``slots``,
    the check falls back to whichever slots currently carry the garment
    (so only half — not off — can un-gate it).

    ``plain`` defaults to the wardrobe's ``default`` garment. Backwards
    compatible: no ``accessory_policy`` → every garment uses ``worn``.
    """
    policy_map = wardrobe.get("accessory_policy")
    policy_map = policy_map if isinstance(policy_map, dict) else {}
    eff = list(garments)
    groups: dict[str, list[int]] = {}
    for i, g in enumerate(garments):
        if g and g not in ("-", DEFAULT_GARMENT):
            groups.setdefault(g, []).append(i)
    for gid, idxs in groups.items():
        rule = policy_map.get(gid)
        if isinstance(rule, dict):
            mode = rule.get("mode") or "worn"
            plain = rule.get("plain") or DEFAULT_GARMENT
        elif isinstance(rule, str):
            mode, plain = rule, DEFAULT_GARMENT
        else:
            mode, plain = "worn", DEFAULT_GARMENT
        if mode == "always":
            active = True
        elif mode == "all_on":
            gate = _policy_gate_indices(rule, idxs)
            active = all(i < len(slots) and slots[i] == 1 for i in gate)
        else:  # "worn"
            active = any(i < len(slots) and slots[i] != 3 for i in idxs)
        if not active:
            for i in idxs:
                eff[i] = plain
    return tuple(eff)


def _wardrobe_layers(
    slots: tuple[int, ...],
    garments: tuple[str, ...],
    wardrobe: dict[str, Any],
    transparency: tuple[int, ...] | None = None,
) -> list[tuple[str, str]]:
    """Resolve layer paths from a wardrobe.json catalog. Returns a list
    of ``(kind, relative_path)`` tuples in render order so the caller
    can map each layer back to a slot for transparency / debugging."""
    by_kind: dict[str, str] = {}

    # Accessory policy: rewrite garments to their plain variant where the
    # clothing state says the baked-in accessories shouldn't render (see
    # `_apply_accessory_policy`). Done up front so the base pick AND the
    # per-slot garment lookups below both use the resolved variant.
    garments = _apply_accessory_policy(slots, garments, wardrobe)

    # Base body — required. `base` is normally a single path string, but it
    # may also be a dict {garment: path} so an outfit renders over the body
    # it was captured against (with that outfit's baked-in accessories).
    # The accessory-policy pass above already swapped in the plain garment
    # where accessories shouldn't show, so a simple first-non-default pick
    # here lands on the right body.
    base = wardrobe.get("base")
    if isinstance(base, dict):
        chosen = next(
            (g for g in garments if g and g not in ("-", DEFAULT_GARMENT)),
            DEFAULT_GARMENT,
        )
        base = base.get(chosen) or base.get(DEFAULT_GARMENT)
    if isinstance(base, str) and base:
        by_kind["base"] = base

    top_state = slots[0]
    bot_state = slots[1]

    # Per-piece transparency. The combined ``top_bottom`` sprite is a
    # single image and fades as one unit (its alpha is multiplied
    # wholesale), so if the top and bottom slots are set to *different*
    # transparency levels — e.g. a wet see-through shirt over opaque
    # trousers — the combo can't honour that and the whole outfit fades
    # together. When the two slots differ, prefer the separate top /
    # bottom layers so each piece fades on its own value, but only when
    # separate art exists for both; otherwise fall back to the combo.
    tr_top = transparency[0] if transparency and len(transparency) > 0 else 100
    tr_bot = transparency[1] if transparency and len(transparency) > 1 else 100
    prefer_separate = tr_top != tr_bot

    combo_path: str | None = None
    if top_state != 3 and bot_state != 3:
        combo = _wardrobe_pick(wardrobe, "top_bottom", garments[0] or garments[1])
        if isinstance(combo, dict):
            cand = combo.get(f"{top_state},{bot_state}")
            if isinstance(cand, str) and cand:
                combo_path = cand

    top_path = (
        _wardrobe_pick_state(wardrobe, "top", garments[0], top_state)
        if top_state != 3 else None
    )
    bot_path = (
        _wardrobe_pick_state(wardrobe, "bottom", garments[1], bot_state)
        if bot_state != 3 else None
    )

    if combo_path and not (prefer_separate and top_path and bot_path):
        # Combined layer: the default, and the fallback when per-piece
        # fading was requested but separate art isn't available.
        by_kind["top_bottom"] = combo_path
    else:
        # Separate layers — either no combo entry, only one slot set, or
        # the two slots fade differently and both have their own art.
        if top_path:
            by_kind["top"] = top_path
        if bot_path:
            by_kind["bottom"] = bot_path

    # Single-slot bra / underwear / legwear.
    for idx, kind in _SINGLE_SLOT_KINDS.items():
        st = slots[idx]
        if st == 3:
            continue
        path = _wardrobe_pick_state(wardrobe, kind, garments[idx], st)
        if path:
            by_kind[kind] = path

    # Always-on overlay — a base-body region (e.g. a hand resting on the
    # hip) that must composite OVER every clothing layer so it isn't hidden
    # behind the outfit. Independent of any slot state. Resolved like
    # `base`: a plain path string, or a {garment: path} map keyed on the
    # chosen non-default garment (so a different-base outfit can ship its
    # own overlay) with a `default` fallback.
    overlay = wardrobe.get("overlay")
    if isinstance(overlay, dict):
        chosen = next(
            (g for g in garments if g and g not in ("-", DEFAULT_GARMENT)),
            DEFAULT_GARMENT,
        )
        overlay = overlay.get(chosen) or overlay.get(DEFAULT_GARMENT)
    if isinstance(overlay, str) and overlay:
        by_kind["overlay"] = overlay

    return [(k, by_kind[k]) for k in _LAYER_ORDER if k in by_kind]


# Legacy slot-name aliases for wardrobe.json files authored before
# the project-wide rename of `panties` → `underwear`. The code now
# looks up "underwear" as the slot key; production wardrobes still
# carry "panties" entries. Try the modern name first; fall back to
# the legacy name so pre-rename wardrobes keep rendering. Same
# fallback pattern as `clothing_v2._LEGACY_SLOT_ALIASES`.
_WARDROBE_LEGACY_SLOT_ALIASES: dict[str, str] = {
    "underwear": "panties",
}


def _wardrobe_pick(
    wardrobe: dict[str, Any], slot: str, garment_id: str
) -> Any:
    """Return the wardrobe entry for a (slot, garment_id) pair, falling
    back to the default garment when the requested id is missing.

    Returns the inner dict (state-name → path) for normal slots, or the
    inner combo dict for top_bottom. None when the slot is missing.
    """
    slot_dict = wardrobe.get(slot)
    # Legacy alias fallback — wardrobes authored before the
    # panties→underwear rename still have "panties" as the slot key.
    if not isinstance(slot_dict, dict):
        legacy = _WARDROBE_LEGACY_SLOT_ALIASES.get(slot)
        if legacy:
            slot_dict = wardrobe.get(legacy)
    if not isinstance(slot_dict, dict):
        return None
    gid = (garment_id or "").strip() or DEFAULT_GARMENT
    inner = slot_dict.get(gid)
    if inner is None and gid != DEFAULT_GARMENT:
        inner = slot_dict.get(DEFAULT_GARMENT)
    return inner


def _wardrobe_pick_state(
    wardrobe: dict[str, Any], slot: str, garment_id: str, state: int
) -> str | None:
    """Return the relative file path for a (slot, garment, state) triple,
    falling back: requested state → "on" → first available state. None
    if no asset exists at all.
    """
    inner = _wardrobe_pick(wardrobe, slot, garment_id)
    if not isinstance(inner, dict):
        return None
    state_name = _STATE_NAMES.get(int(state))
    if state_name and isinstance(inner.get(state_name), str):
        return inner[state_name]
    if isinstance(inner.get("on"), str):
        return inner["on"]
    for v in inner.values():
        if isinstance(v, str) and v:
            return v
    return None


def _legacy_layers(slots: tuple[int, ...]) -> list[str]:
    """Resolve layer paths via the hard-coded 14-image catalog."""
    s = slots
    layers_by_kind: dict[str, int] = {"base": _BASE_CATALOG[(3, 3, 3, 3, 3, 3, 3, 3)]}

    if s[0] != 3 and s[1] != 3:
        combo_key = (s[0], s[1], 3, 3, 3, 3, 3, 3)
        if combo_key in _BASE_CATALOG:
            layers_by_kind["top_bottom"] = _BASE_CATALOG[combo_key]
    if "top_bottom" not in layers_by_kind:
        if s[0] != 3:
            top_key = (s[0], 3, 3, 3, 3, 3, 3, 3)
            if top_key in _BASE_CATALOG:
                layers_by_kind["top"] = _BASE_CATALOG[top_key]
        if s[1] != 3:
            bot_key = (3, s[1], 3, 3, 3, 3, 3, 3)
            if bot_key in _BASE_CATALOG:
                layers_by_kind["bottom"] = _BASE_CATALOG[bot_key]

    for idx, kind in _SINGLE_SLOT_KINDS.items():
        if s[idx] == 3:
            continue
        key = tuple(s[i] if i == idx else 3 for i in range(8))
        if key in _BASE_CATALOG:
            layers_by_kind[kind] = _BASE_CATALOG[key]

    return [
        (k, f"Image{layers_by_kind[k]}.png")
        for k in _LAYER_ORDER
        if k in layers_by_kind
    ]


def match_layers(slots: Iterable[int]) -> list[int]:
    """Legacy helper retained for any external caller. Returns the 1-14
    image numbers picked by the legacy catalog. Wardrobe-mode callers
    should use ``_resolve_layer_paths`` instead."""
    s = tuple(int(x) for x in slots)
    if len(s) != 8:
        raise ValueError(f"expected 8 slots, got {len(s)}")
    layers_by_kind: dict[str, int] = {"base": _BASE_CATALOG[(3, 3, 3, 3, 3, 3, 3, 3)]}
    if s[0] != 3 and s[1] != 3:
        combo_key = (s[0], s[1], 3, 3, 3, 3, 3, 3)
        if combo_key in _BASE_CATALOG:
            layers_by_kind["top_bottom"] = _BASE_CATALOG[combo_key]
    if "top_bottom" not in layers_by_kind:
        if s[0] != 3:
            top_key = (s[0], 3, 3, 3, 3, 3, 3, 3)
            if top_key in _BASE_CATALOG:
                layers_by_kind["top"] = _BASE_CATALOG[top_key]
        if s[1] != 3:
            bot_key = (3, s[1], 3, 3, 3, 3, 3, 3)
            if bot_key in _BASE_CATALOG:
                layers_by_kind["bottom"] = _BASE_CATALOG[bot_key]
    for idx, kind in _SINGLE_SLOT_KINDS.items():
        if s[idx] == 3:
            continue
        key = tuple(s[i] if i == idx else 3 for i in range(8))
        if key in _BASE_CATALOG:
            layers_by_kind[kind] = _BASE_CATALOG[key]
    return [layers_by_kind[k] for k in _LAYER_ORDER if k in layers_by_kind]


# ---------------------------------------------------------------------------
# Internals — files & cache
# ---------------------------------------------------------------------------


_WARDROBE_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _load_wardrobe(
    assets: Path, sprite_id: str
) -> dict[str, Any] | None:
    """Read ``<assets>/<sprite_id>/wardrobe.json`` if present. Cached by
    mtime so edits in dev are picked up without restarting the app."""
    path = assets / sprite_id / "wardrobe.json"
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cache_key = str(path)
    cached = _WARDROBE_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _WARDROBE_CACHE[cache_key] = (mtime, None)
        return None
    if not isinstance(data, dict):
        data = None
    _WARDROBE_CACHE[cache_key] = (mtime, data)
    return data


def _assets_dir() -> Path:
    raw = current_app.config.get("sprite_assets_dir") or "Temp characters/new image addon junk"
    p = Path(raw)
    if not p.is_absolute():
        # Resolve relative to the project root (parent of the `app/` dir).
        p = Path(current_app.root_path).parent / p
    return p


def _mtime_ns(path: Path) -> int:
    """Last-modified time of `path` in ns, or 0 if it can't be stat'd.
    Used to make cache keys content-sensitive: re-exporting a layer PNG
    in place (same filename) bumps its mtime, so the key changes and the
    stale composite is recomposed instead of served from cache."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _cache_key(
    sprite_id: str,
    slots: tuple[int, ...],
    garments: tuple[str, ...] | None,
    scene_tag: str,
    layer_paths: list[tuple[str, str]],
    *,
    crop_solo: bool = False,
    transparency: tuple[int, ...] | None = None,
) -> str:
    assets = _assets_dir()
    g_str = ",".join(garments or ())
    layers_str = ",".join(rel for _, rel in layer_paths)
    tr_str = ",".join(str(int(t)) for t in (transparency or ()))
    # Stat every resolved layer + the scene so an in-place file edit
    # (same filename) changes the key and busts the cached PNG.
    sigs = [str(_mtime_ns(assets / sprite_id / rel)) for _, rel in layer_paths]
    sigs.append(str(_mtime_ns(assets / "Scene" / f"{scene_tag}.png")))
    raw = (
        f"{sprite_id}|{','.join(map(str, slots))}|garments={g_str}|"
        f"{scene_tag}|layers={layers_str}|crop_solo={int(bool(crop_solo))}|"
        f"transparency={tr_str}|mtimes={','.join(sigs)}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _multi_cache_key(
    participants: list[tuple[str, tuple[int, ...], tuple[str, ...] | None]],
    scene_tag: str,
    resolved: list[tuple[str, list[tuple[str, str]]]],
    *,
    transparency_per: list[tuple[int, ...] | None] | None = None,
) -> str:
    assets = _assets_dir()
    scene_sig = str(_mtime_ns(assets / "Scene" / f"{scene_tag}.png"))
    parts: list[str] = [f"{scene_tag}:{scene_sig}"]
    for i, ((sprite_id, slots, garments), (_, layer_paths)) in enumerate(zip(participants, resolved)):
        g_str = ",".join(garments or ())
        layers_str = ",".join(rel for _, rel in layer_paths)
        tr = (transparency_per[i] if transparency_per else None) or ()
        tr_str = ",".join(str(int(t)) for t in tr)
        # Per-layer mtime so a re-exported PNG invalidates the composite.
        sigs = ",".join(str(_mtime_ns(assets / sprite_id / rel)) for _, rel in layer_paths)
        parts.append(
            f"{sprite_id}|{','.join(map(str, slots))}|g={g_str}|"
            f"layers={layers_str}|tr={tr_str}|mtimes={sigs}"
        )
    raw = "||".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]
