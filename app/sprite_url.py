"""Sprite-URL image system.

Characters declare an image format through ``properties.images``:

  {"format": "combined", "sprite_id": "Iris1"}      # local layered sprites
  {"format": "tagged",   "entries": [...]}          # remote catalog (pose-per-URL style)

For ``combined`` characters, the engine builds the image URL
deterministically from current state — no model call needed — in the
format:

  <host>/sprites/<sprite_id>/<slots>/<Scene>
  <host>/sprites/<sprite_id>/<slots>/<garments>/<Scene>

The optional ``<garments>`` segment names which named version of each
slot's asset to render (per the wardrobe.json catalog in the sprite
directory). When every garment is the default the segment is omitted
and the legacy three-segment URL is emitted, so cached renders for
unmigrated characters keep their old keys.

When ``host`` is empty the URL is relative (``/sprites/...``) — the
current Flask app serves the composed PNG itself via
``app/routes/sprites.py`` + ``app/sprite_compose.py``. The ``host``
override is for cases where someone wants to point at a remote sprite
server instead of the local one.

Slot values: 1 = on, 2 = partially removed, 3 = off. Defaults to 1 when
the current outfit doesn't declare the slot, so a character without a
configured outfit still renders cleanly.

Inputs come from existing entity state:
  - sprite_id  -> character.properties.images.sprite_id
                  (legacy: properties.sprite_id is still read)
  - clothing slots -> outfit.properties.clothing_slots (resolved through
                      the outfit's ``extends`` chain, like every other
                      outfit field)
  - scene tag  -> room.properties.scene_tag, falling back to the parent
                  location's properties.scene_tag, falling back to a
                  default

The legacy ``properties.image_pack`` (tagged flat catalog) keeps
working unchanged — legacy tagged characters have not been migrated. New characters should
use ``properties.images`` going forward; the helpers below treat the
new field as canonical and fall back to the old fields only when it is
absent.
"""
from __future__ import annotations

from typing import Any


SLOT_ORDER = ("top", "bottom", "bra", "underwear", "pantyhose", "gloves", "legwear", "shoes")
DEFAULT_SCENE = "Apartment"


def image_format(character: dict[str, Any] | None) -> str | None:
    """Return the declared image format for `character`, or None.

    Reads ``properties.images.format`` first; falls back to inferring
    from the legacy fields (``sprite_id`` -> "combined", ``image_pack``
    -> "tagged"). Returns one of ``"combined"`` / ``"tagged"`` / None.
    """
    if not character:
        return None
    props = character.get("properties") or {}
    images = props.get("images")
    if isinstance(images, dict):
        fmt = (images.get("format") or "").strip().lower()
        if fmt in ("combined", "tagged"):
            return fmt
    # Legacy inference: only used when `images` is missing entirely.
    if (props.get("sprite_id") or "").strip():
        return "combined"
    pack = props.get("image_pack")
    if isinstance(pack, dict) and pack.get("entries"):
        return "tagged"
    # Named non-composed image packs (multi-enable, additive) also mark a
    # character as tagged even when no flat `images` block is present.
    packs = props.get("image_packs")
    if isinstance(packs, dict) and packs:
        return "tagged"
    return None


def sprite_id_of(character: dict[str, Any] | None) -> str | None:
    """Return the sprite directory name for a ``combined``-format
    character, or None.

    Prefers ``properties.images.sprite_id``; falls back to the legacy
    top-level ``properties.sprite_id``.
    """
    if not character:
        return None
    props = character.get("properties") or {}
    images = props.get("images")
    if isinstance(images, dict):
        sid = (images.get("sprite_id") or "").strip()
        if sid:
            return sid
    sid = (props.get("sprite_id") or "").strip()
    return sid or None


def image_packs_of(character: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Named non-composed image packs declared on a character at::

        properties.image_packs = {
          "<pack_id>": {
            "name": "Generic",
            "default_enabled": true,
            "entries": [{"caption": ..., "image_url": ...}, ...]
          },
          ...
        }

    Each pack is an independent catalog of tagged entries (the same shape
    as ``images.entries``). Packs are *additive* and *multi-enable* — like
    clothing, any subset can be active at once and their entries union into
    the per-turn pick catalog. Returns ``{}`` when nothing is declared.
    """
    if not character:
        return {}
    packs = (character.get("properties") or {}).get("image_packs")
    if isinstance(packs, dict):
        return {k: v for k, v in packs.items() if isinstance(v, dict)}
    return {}


def enabled_image_packs_of(
    character: dict[str, Any] | None,
    scene_tags: set[str] | None = None,
) -> list[str]:
    """Pack ids currently enabled for ``character``.

    The active set is the union of:
      - the **base** set: the instance-scoped ``properties.enabled_image_packs``
        list when present (set by the staging panel or a
        ``[set ... enabled_image_packs]`` directive, branch-local — the global
        card is never mutated); when that field is absent, every pack flagged
        ``default_enabled``; AND
      - any pack **tag-exposed** by the scene: a pack may declare
        ``expose_tags: ["<tag>", ...]`` and is auto-enabled whenever one of
        those tags is present in ``scene_tags`` (the room / outfit / object /
        scenario tags live in the scene). This is the image-side mirror of
        how ``conditional_pairs`` surface from scene tags — e.g. a
        costume-party object's tag exposes the matching pack.

    Unknown pack ids are ignored.
    """
    if not character:
        return []
    props = character.get("properties") or {}
    packs = image_packs_of(character)
    enabled = props.get("enabled_image_packs")
    if isinstance(enabled, list):
        active = [p for p in enabled if isinstance(p, str) and p in packs]
    else:
        active = [pid for pid, pk in packs.items() if pk.get("default_enabled")]
    # Tag-exposed packs join the active set (additive, dedup-preserving order).
    scene = {str(t).lower() for t in (scene_tags or set())}
    if scene:
        for pid, pk in packs.items():
            if pid in active:
                continue
            want = {str(t).lower() for t in (pk.get("expose_tags") or [])}
            if want & scene:
                active.append(pid)
    return active


def tagged_entries_of(
    character: dict[str, Any] | None,
    scene_tags: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the catalog entries for a ``tagged``-format character.

    The catalog is the union of:
      - the flat base list (``properties.images.entries``, or the legacy
        ``properties.image_pack.entries`` legacy tagged characters still use), treated as an
        always-on base pack — unless the branch-scoped
        ``properties.base_images_enabled`` is explicitly ``false`` (the
        cast panel's "Default images" toggle), AND
      - the entries of every *enabled* named pack in
        ``properties.image_packs`` (see ``enabled_image_packs_of``) — where
        "enabled" includes packs tag-exposed by ``scene_tags``.

    Enabled packs are additive — they extend the base catalog rather than
    replacing it — so the per-turn picker scores across everything active.
    ``scene_tags`` is optional; without it only the base + default/explicitly
    enabled packs apply (no tag-exposed packs).
    """
    if not character:
        return []
    props = character.get("properties") or {}
    base: list[dict[str, Any]] = []
    if props.get("base_images_enabled") is not False:
        images = props.get("images")
        if isinstance(images, dict) and isinstance(images.get("entries"), list):
            base = [e for e in images["entries"] if isinstance(e, dict)]
        else:
            pack = props.get("image_pack")
            if isinstance(pack, dict):
                base = [e for e in (pack.get("entries") or []) if isinstance(e, dict)]
    # Union the enabled named packs on top of the base catalog.
    packs = image_packs_of(character)
    for pid in enabled_image_packs_of(character, scene_tags):
        pk = packs.get(pid) or {}
        base += [e for e in (pk.get("entries") or []) if isinstance(e, dict)]
    return base


def outfit_profiles_of(character: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Named tag-bundles a tagged-format character can be pinned to.

    Shape::

        properties.images.outfit_profiles = {
          "casual":  {"name": "Casual",       "required_tags": ["tshirt"]},
          "stage":   {"name": "Stage outfit", "required_tags": ["concert"]},
          "bikini":  {"name": "Bikini",       "required_tags": ["bikini"]},
        }

    Returns ``{}`` when nothing is declared. ``required_tags`` is matched
    as case-insensitive substrings against each entry's caption — the
    captions are danbooru-style comma lists, so substring is more
    forgiving than exact-token (``"bikini"`` matches
    ``"blue and white stripped bikini"``).
    """
    if not character:
        return {}
    props = character.get("properties") or {}
    images = props.get("images")
    if isinstance(images, dict):
        profiles = images.get("outfit_profiles")
        if isinstance(profiles, dict):
            return {k: v for k, v in profiles.items() if isinstance(v, dict)}
    pack = props.get("image_pack")
    if isinstance(pack, dict):
        profiles = pack.get("outfit_profiles")
        if isinstance(profiles, dict):
            return {k: v for k, v in profiles.items() if isinstance(v, dict)}
    return {}


def current_outfit_profile_of(character: dict[str, Any] | None) -> str | None:
    """Active outfit-profile id for a tagged-format character.

    Lives on the character entity at ``properties.current_outfit_profile``
    so it follows the same write path as ``current_outfit`` for
    combined-format characters.
    """
    if not character:
        return None
    props = character.get("properties") or {}
    pid = props.get("current_outfit_profile")
    return pid.strip() if isinstance(pid, str) and pid.strip() else None


def filter_entries_by_profile(
    entries: list[dict[str, Any]], profile: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Return entries whose caption contains every ``required_tags``
    string (case-insensitive substring). When the profile is missing,
    has no required_tags, or filtering yields zero entries, return the
    list unchanged so the picker never deadlocks on a typo'd profile."""
    if not profile:
        return entries
    required = profile.get("required_tags") or []
    required = [t.strip().lower() for t in required if isinstance(t, str) and t.strip()]
    if not required:
        return entries
    survivors = [
        e for e in entries
        if all(t in (e.get("caption") or "").lower() for t in required)
    ]
    return survivors or entries


def image_pool_states_of(character: dict[str, Any] | None) -> dict[str, str]:
    """Branch-scoped per-variant pool state for tagged-format characters.

    ``properties.image_pool_states`` maps an outfit_profile (variant) id to a
    tri-state: ``"off"`` (don't pull from this pool) or ``"excluded"`` (remove
    this pool's images from every pool). Anything absent defaults to ``"on"``.
    """
    if not character:
        return {}
    states = (character.get("properties") or {}).get("image_pool_states")
    if not isinstance(states, dict):
        return {}
    return {k: v for k, v in states.items() if v in ("off", "excluded")}


def _profile_matches(entry: dict[str, Any], required: list[str]) -> bool:
    cap = (entry.get("caption") or "").lower()
    return all(t in cap for t in required)


def filter_entries_by_pools(
    entries: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    states: dict[str, str],
) -> list[dict[str, Any]]:
    """Apply the tri-state pool selection to a catalog.

    For each entry, find the profiles (variants) it matches (required_tags is a
    caption subset). An entry is eligible iff:
      - NO matching profile is ``excluded``, AND
      - it matches no profile at all (uncovered → available by default), OR at
        least one matching profile is ``on`` (state absent / not off/excluded).

    So an ``off`` variant drops the images unique to it, while ``excluded``
    removes its images even when they're shared with an ``on`` pool. Empty
    result falls back to the full catalog so the picker never deadlocks.
    """
    if not profiles:
        return entries
    # Pre-normalise each profile's required tags once.
    norm = {
        pid: [t.strip().lower() for t in (prof.get("required_tags") or [])
              if isinstance(t, str) and t.strip()]
        for pid, prof in profiles.items()
    }
    out: list[dict[str, Any]] = []
    for e in entries:
        matched_states = [
            states.get(pid, "on")
            for pid, req in norm.items()
            if _profile_matches(e, req)
        ]
        if any(s == "excluded" for s in matched_states):
            continue
        if not matched_states or any(s == "on" for s in matched_states):
            out.append(e)
    return out or entries


def has_sprite(character: dict[str, Any] | None) -> bool:
    return image_format(character) == "combined" and bool(sprite_id_of(character))


def build_group_url(
    *,
    host: str,
    participants: list[dict[str, Any]],
    scene_tag: str | None,
) -> str:
    """Return the side-by-side multi-character composite URL.

    ``participants`` is an ordered list of ``{sprite_id, clothing_slots,
    garments}`` dicts (lead first, then partners in chain order). The
    returned URL is served by ``app/routes/sprites.py`` (route
    ``/sprites/group/<scene>/<spec>/<spec>/...``) and rendered by
    ``sprite_compose.compose_multi_png`` — characters are pasted at
    column-center offsets over a single shared scene background.

    Each spec encodes one character as ``<sprite_id>:<slots>`` or, when
    any garment diverges from the wardrobe default,
    ``<sprite_id>:<slots>:<garments>`` (slots/garments use ``,`` as the
    inner separator; ``:`` separates the three fields). Default-only
    garment maps are omitted so cache keys stay stable for
    unmigrated characters.
    """
    base = (host or "").rstrip("/")
    scene = (scene_tag or DEFAULT_SCENE).strip() or DEFAULT_SCENE
    specs: list[str] = []
    for p in participants:
        sprite_id = (p.get("sprite_id") or "").strip()
        if not sprite_id:
            continue
        slots_csv = ",".join(_slot_value(p.get("clothing_slots"), name) for name in SLOT_ORDER)
        g_csv = _garments_csv(p.get("garments"))
        tr_csv = _transparency_csv(p.get("transparency"))
        # Always emit garments segment when transparency is set so the
        # 4th colon-segment lines up with the route's parser
        # (sprite_id:slots:garments:transparency).
        if tr_csv and not g_csv:
            g_csv = "-,-,-,-,-,-,-,-"
        spec = f"{sprite_id}:{slots_csv}"
        if g_csv:
            spec = f"{spec}:{g_csv}"
        if tr_csv:
            spec = f"{spec}:{tr_csv}"
        specs.append(spec)
    if not specs:
        # No participants — degenerate; just point at the scene-only fallback
        # so the route serves a plain scene PNG instead of 404'ing.
        return f"{base}/sprites/group/{scene}"
    return f"{base}/sprites/group/{scene}/" + "/".join(specs)


def build_url(
    *,
    host: str,
    sprite_id: str,
    clothing_slots: dict[str, Any] | None,
    scene_tag: str | None,
    garments: dict[str, Any] | None = None,
    transparency: dict[str, Any] | None = None,
) -> str:
    """Return the composed sprite URL.

    Empty `host` yields a relative URL served by the local Flask
    blueprint at ``app/routes/sprites.py``. A non-empty `host` is
    prefixed verbatim — set it to point at a remote sprite server
    instead. Trailing slashes on `host` are stripped. `clothing_slots`
    may be partial — missing slots default to 1 (on). `scene_tag` falls
    back to DEFAULT_SCENE.

    `garments` (slot-name → garment-id) is optional. When every slot is
    the default (or the dict is None / empty) the legacy three-segment
    URL is emitted; otherwise a four-segment URL with a per-slot CSV is
    produced where un-set slots are encoded as ``-``.
    """
    base = (host or "").rstrip("/")
    csv = ",".join(_slot_value(clothing_slots, name) for name in SLOT_ORDER)
    scene = (scene_tag or DEFAULT_SCENE).strip() or DEFAULT_SCENE
    g_csv = _garments_csv(garments)
    tr_csv = _transparency_csv(transparency)
    if g_csv:
        url = f"{base}/sprites/{sprite_id}/{csv}/{g_csv}/{scene}"
    else:
        url = f"{base}/sprites/{sprite_id}/{csv}/{scene}"
    if tr_csv:
        url = f"{url}?tr={tr_csv}"
    return url


def resolve_scene_tag(
    *,
    room: dict[str, Any] | None,
    location: dict[str, Any] | None,
    character: dict[str, Any] | None,
) -> str:
    """Pick the scene PNG to render against for a (room, location, character).

    Resolution order (first match wins):

      1. Explicit ``properties.scene_tag`` string on the room, then on
         the location. Authoritative — used to force a specific scene.
         The lookup tries the character's ``scene_prefix`` first
         (``<prefix>_<tag>.png``) and then the bare tag, both
         case-insensitive against the actual filenames.
      2. Tag-overlap against ``Scene/manifest.json``: the candidate is
         the manifest entry with the highest overlap between its
         ``tags`` and ``room.tags ∪ location.tags``. Manifest entries
         with a ``for`` field are gated to a matching character
         ``scene_prefix`` (entries owned by a different character are
         excluded). When two scenes tie on overlap, an owner-matching
         scene wins — so Rosa in a bedroom-tagged room gets
         ``Rosa_bedroom`` over ``Female_bedroom``, but in a
         kitchen-tagged room she still gets ``Apartment`` because her
         bedroom scene's overlap with kitchen tags is too low to win.
      3. ``DEFAULT_SCENE``.

    The Scene listing and manifest are loaded from
    ``current_app.config['sprite_assets_dir']`` (or the legacy
    ``Temp characters/new image addon junk`` path) and lightly cached.
    Callers without a Flask context get the default.
    """
    available = _available_scenes()
    manifest = _scene_manifest()
    char_prefix = _character_scene_prefix(character)

    # Pass 1: explicit scene_tag string — author override.
    for entity in (room, location):
        if not isinstance(entity, dict):
            continue
        legacy = (entity.get("properties") or {}).get("scene_tag")
        if isinstance(legacy, str) and legacy.strip():
            match = _try_scene(legacy, char_prefix, available)
            if match:
                return match

    # Pass 2: tag-overlap against the manifest. Room tags weight 2 and
    # location tags weight 1, so a room's own character (an outdoor
    # balcony, a school locker room) outweighs the parent location's
    # broader vibe.
    weighted: dict[str, int] = {}
    if isinstance(location, dict):
        for t in (location.get("tags") or []):
            if isinstance(t, str):
                weighted[t.strip().lower()] = 1
    if isinstance(room, dict):
        for t in (room.get("tags") or []):
            if isinstance(t, str):
                weighted[t.strip().lower()] = 2
    if manifest and weighted:
        ranked: list[tuple[int, int, str]] = []
        for scene_name, info in manifest.items():
            if not isinstance(info, dict):
                continue
            owner = (info.get("for") or "").strip()
            if owner and owner.lower() != char_prefix.lower():
                continue
            scene_tags = {
                t.strip().lower()
                for t in (info.get("tags") or [])
                if isinstance(t, str)
            }
            score = sum(weighted[t] for t in scene_tags if t in weighted)
            if score == 0:
                continue
            owner_match = 1 if owner else 0
            ranked.append((score, owner_match, scene_name))
        ranked.sort(key=lambda r: (-r[0], -r[1]))
        for _, _, name in ranked:
            if _scene_exists(name, available):
                return name

    # Pass 3: default.
    return DEFAULT_SCENE


def garments_of(outfit: dict[str, Any] | None) -> dict[str, str]:
    """Extract the ``garments`` map from a resolved outfit. Empty dict
    when the outfit doesn't declare one (every slot uses the wardrobe's
    `default` garment)."""
    if not isinstance(outfit, dict):
        return {}
    props = outfit.get("properties") or {}
    g = props.get("garments")
    if not isinstance(g, dict):
        return {}
    return {k: str(v) for k, v in g.items() if isinstance(k, str) and isinstance(v, str)}


def caption_for(
    *,
    character: dict[str, Any],
    outfit: dict[str, Any] | None,
    room: dict[str, Any] | None,
    location: dict[str, Any] | None,
) -> str:
    """Short human-readable caption to store alongside the URL.

    Shape mirrors what `image_pack_pick` writes so the frontend renderer
    (chat.js) can treat both the same way (alt text, hover, exports).
    """
    parts = [character.get("name") or character.get("id") or "Character"]
    if outfit:
        op = outfit.get("properties") or {}
        text = (
            op.get("concise_description")
            or outfit.get("name")
            or op.get("intact_description")
        )
        if text:
            parts.append(str(text).strip())
    place = (room or {}).get("name") or (location or {}).get("name")
    if place:
        parts.append(str(place).strip())
    return " — ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _transparency_csv(transparency: dict[str, Any] | None) -> str:
    """Per-slot transparency CSV (0..100). Slots at 100 / unset become
    ``-``. Returns ``""`` when nothing is below 100 — callers skip the
    optional segment entirely. Mirrors `sprite_compose.transparency_csv`
    but kept inline so URL building doesn't import the compositor."""
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


def _garments_csv(garments: dict[str, Any] | None) -> str:
    """Return a CSV of garment ids in canonical slot order, or "" if
    every slot is default. Slots without an id are encoded as "-"."""
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
        if isinstance(raw, str):
            cleaned = raw.strip().replace("..", "").replace("/", "").replace("\\", "")
            if cleaned and cleaned != "-":
                parts.append(cleaned)
                any_set = True
                continue
        parts.append("-")
    return ",".join(parts) if any_set else ""


def _character_scene_prefix(character: dict[str, Any] | None) -> str:
    if not isinstance(character, dict):
        return ""
    images = (character.get("properties") or {}).get("images")
    if isinstance(images, dict):
        prefix = images.get("scene_prefix")
        if isinstance(prefix, str):
            return prefix.strip()
    return ""


def _try_scene(
    tag: str, char_prefix: str, available: set[str]
) -> str | None:
    """Try the prefixed scene first, then the bare tag. Case-insensitive
    matching against the actual filenames; returns the canonical name as
    it appears on disk so the URL is stable."""
    if char_prefix:
        prefixed = f"{char_prefix}_{tag.strip()}"
        canon = _canonical_scene_name(prefixed, available)
        if canon:
            return canon
    return _canonical_scene_name(tag.strip(), available)


def _canonical_scene_name(tag: str, available: set[str]) -> str | None:
    if not tag:
        return None
    lower = tag.lower()
    for name in available:
        if name.lower() == lower:
            return name
    return None


def _scene_exists(name: str, available: set[str]) -> bool:
    return _canonical_scene_name(name, available) is not None


# Lazy-cached views of the Scene/ directory. Reloaded when its mtime
# changes so dev edits show up without a restart; in production it's
# essentially a one-shot read.
_SCENE_LIST_CACHE: dict[str, tuple[float, set[str]]] = {}
_SCENE_MANIFEST_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _scene_assets_dir():
    try:
        from flask import current_app
        from pathlib import Path

        raw = current_app.config.get("sprite_assets_dir") or "Temp characters/new image addon junk"
        p = Path(raw)
        if not p.is_absolute():
            p = Path(current_app.root_path).parent / p
        return p / "Scene"
    except Exception:
        return None


def _available_scenes() -> set[str]:
    d = _scene_assets_dir()
    if d is None or not d.exists():
        return set()
    try:
        mtime = d.stat().st_mtime
    except OSError:
        return set()
    key = str(d)
    cached = _SCENE_LIST_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    names: set[str] = set()
    try:
        for entry in d.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".png":
                names.add(entry.stem)
    except OSError:
        pass
    _SCENE_LIST_CACHE[key] = (mtime, names)
    return names


def _scene_manifest() -> dict[str, Any]:
    d = _scene_assets_dir()
    if d is None:
        return {}
    path = d / "manifest.json"
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    key = str(path)
    cached = _SCENE_MANIFEST_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    # Strip the ``_comment`` key authors use to annotate the manifest.
    data = {k: v for k, v in data.items() if not str(k).startswith("_")}
    _SCENE_MANIFEST_CACHE[key] = (mtime, data)
    return data


def _slot_value(slots: dict[str, Any] | None, name: str) -> str:
    """Read a single slot, normalising to "1" / "2" / "3"."""
    if not isinstance(slots, dict):
        return "1"
    raw = slots.get(name)
    if raw is None:
        # case-insensitive fallback so authoring is forgiving
        for k, v in slots.items():
            if isinstance(k, str) and k.lower() == name:
                raw = v
                break
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return "1"
    if n in (1, 2, 3):
        return str(n)
    return "1"
