"""JSON API for entities, conversations, and pending edits."""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request

from .. import conversations as convs
from .. import entities as ent
from ..auth import login_required
from ..config import save_local_overrides
from ..ollama_client import chat_sync, list_loaded, model_names, test_connection, warmup
from ..effective import (
    active_setup_for_path,
    active_setup_root_for_path,
    default_responder_for_path,
    effective_cast_at,
    effective_entities_at,
    effective_user_persona,
    path_applied_edits_with_origin,
)
from ..personas import assemble_prompt
from .. import sprite_url as sprite


bp = Blueprint("api", __name__)


# ---------------------------------------------------------------------------
# Entity templates (data/)
# ---------------------------------------------------------------------------


@bp.get("/entities")
@login_required
def list_entities():
    return jsonify({"entities": list(ent.load_all().values())})


@bp.get("/entities/<entity_id>")
@login_required
def get_entity(entity_id: str):
    e = ent.get(entity_id)
    if not e:
        return jsonify({"error": "not found"}), 404
    return jsonify(e)


@bp.post("/entities")
@login_required
def create_entity():
    payload = request.get_json(silent=True) or {}
    try:
        saved = ent.save(payload)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(saved), 201


@bp.put("/entities/<entity_id>")
@login_required
def update_entity(entity_id: str):
    payload = request.get_json(silent=True) or {}
    payload["id"] = entity_id
    try:
        saved = ent.save(payload)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(saved)


@bp.delete("/entities/<entity_id>")
@login_required
def delete_entity(entity_id: str):
    ok = ent.delete(entity_id)
    return jsonify({"deleted": ok})


@bp.post("/preview/body")
@login_required
def preview_body():
    """Render the exact character-card appearance block for a character as
    it is being edited — including its (possibly unsaved) `worn` map — so
    the Studio can show what the prompt will actually say. Accepts the live
    character entity; overlays it on the template set so its worn pieces
    resolve, then runs the real _character_card path."""
    from .. import personas
    payload = request.get_json(silent=True) or {}
    char = payload.get("character")
    if not isinstance(char, dict) or not char.get("id"):
        return jsonify({"error": "character object required"}), 400
    # Copy the map before overlaying the unsaved character — load_all()
    # returns the shared, process-cached entities dict, and mutating it in
    # place would leak this request's (client-supplied) character into
    # every other request the worker serves.
    entities = dict(ent.load_all())
    entities[char["id"]] = char  # unsaved edits win over the on-disk copy
    name = char.get("name") or char.get("id")
    ctx = {"user_name": payload.get("user_name") or "You", "char_name": name}
    try:
        card = personas._character_card(char, entities, ctx)
    except Exception as e:  # never 500 the editor over a render hiccup
        return jsonify({"error": f"render failed: {e}"}), 400
    # Pull out just the clothing/appearance lines for a focused view; keep
    # the full card too.
    lines = card.splitlines()
    appearance, grab = [], False
    for ln in lines:
        if ln.startswith(("Currently wearing:", "Appearance:", "Accessories:",
                          "Body marks:")) or ln.startswith("NOT wearing"):
            grab = True
        elif grab and ln and not ln.startswith(" ") and ln.endswith(":") \
                and not ln.startswith("Appearance"):
            grab = False
        if grab:
            appearance.append(ln)
    return jsonify({"card": card, "appearance": "\n".join(appearance).strip()})


@bp.post("/entities/<entity_id>/portrait")
@login_required
def upload_portrait(entity_id: str):
    """Save an uploaded image into the character's folder and update
    properties.portrait. Accepts multipart with a single 'file' field."""
    from pathlib import Path
    e = ent.get(entity_id)
    if not e or e.get("type") != "character":
        return jsonify({"error": "character not found"}), 404
    if "file" not in request.files:
        return jsonify({"error": "missing file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return jsonify({"error": f"unsupported extension {ext}"}), 400
    folder = Path(current_app.config["data_dir"]) / "characters" / entity_id
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"portrait{ext}"
    f.save(folder / filename)
    e.setdefault("properties", {})["portrait"] = filename
    saved = ent.save(e)
    return jsonify({"entity": saved, "portrait": filename})


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _safe_image_name(folder, raw_name: str) -> str:
    """A collision-free, path-safe filename for the character images dir."""
    from pathlib import Path
    import re
    stem = Path(raw_name or "image").stem
    ext = Path(raw_name or "").suffix.lower()
    if ext not in _IMAGE_EXTS:
        ext = ".png"
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", stem).strip("_") or "image"
    candidate = f"{stem}{ext}"
    i = 1
    while (folder / candidate).exists():
        candidate = f"{stem}_{i}{ext}"
        i += 1
    return candidate


@bp.post("/entities/<entity_id>/images")
@login_required
def upload_character_image(entity_id: str):
    """Upload a tagged image for a character into
    data/characters/<id>/images/ and append a {caption, image_url} entry
    to the base catalog (properties.images.entries) or, when a `pack`
    form field is given, to properties.image_packs[pack].entries.

    Multipart form: file (required), caption (optional), pack (optional
    pack id; blank/omitted = base catalog)."""
    from pathlib import Path
    e = ent.get(entity_id)
    if not e or e.get("type") != "character":
        return jsonify({"error": "character not found"}), 404
    if "file" not in request.files:
        return jsonify({"error": "missing file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    if Path(f.filename).suffix.lower() not in _IMAGE_EXTS:
        return jsonify({"error": f"unsupported extension {Path(f.filename).suffix}"}), 400
    folder = Path(current_app.config["data_dir"]) / "characters" / entity_id / "images"
    folder.mkdir(parents=True, exist_ok=True)
    filename = _safe_image_name(folder, f.filename)
    f.save(folder / filename)
    url = f"/character_images/{entity_id}/{filename}"
    entry = {"caption": (request.form.get("caption") or "").strip(), "image_url": url}
    props = e.setdefault("properties", {})
    pack = (request.form.get("pack") or "").strip()
    if pack:
        packs = props.setdefault("image_packs", {})
        pk = packs.get(pack)
        if not isinstance(pk, dict):
            pk = {"name": pack, "default_enabled": False, "entries": []}
            packs[pack] = pk
        pk.setdefault("entries", []).append(entry)
    else:
        images = props.setdefault("images", {})
        if not images.get("format"):
            images["format"] = "tagged"
        images.setdefault("entries", []).append(entry)
    saved = ent.save(e)
    return jsonify({"entity": saved, "image_url": url, "entry": entry})


@bp.post("/entities/<entity_id>/images/delete")
@login_required
def delete_character_image(entity_id: str):
    """Remove a tagged image entry (by image_url) from the base catalog
    and/or any pack, and delete the on-disk file when nothing else
    references it. JSON body: {image_url}."""
    from pathlib import Path
    e = ent.get(entity_id)
    if not e or e.get("type") != "character":
        return jsonify({"error": "character not found"}), 404
    payload = request.get_json(silent=True) or {}
    url = (payload.get("image_url") or "").strip()
    if not url:
        return jsonify({"error": "image_url required"}), 400
    props = e.get("properties") or {}

    def _drop(entries):
        if not isinstance(entries, list):
            return entries, 0
        kept = [x for x in entries if not (isinstance(x, dict) and x.get("image_url") == url)]
        return kept, len(entries) - len(kept)

    removed = 0
    images = props.get("images")
    if isinstance(images, dict):
        images["entries"], n = _drop(images.get("entries"))
        removed += n
    packs = props.get("image_packs")
    if isinstance(packs, dict):
        for pid, pk in packs.items():
            if isinstance(pk, dict):
                pk["entries"], n = _drop(pk.get("entries"))
                removed += n

    # Delete the file only if it lived in this character's images dir and
    # is no longer referenced anywhere on the (now-pruned) card.
    still_referenced = json.dumps(props).find(url) != -1
    fname = url.rsplit("/", 1)[-1]
    expected_prefix = f"/character_images/{entity_id}/"
    if not still_referenced and url.startswith(expected_prefix):
        fpath = Path(current_app.config["data_dir"]) / "characters" / entity_id / "images" / fname
        try:
            if fpath.is_file():
                fpath.unlink()
        except OSError:
            pass
    saved = ent.save(e)
    return jsonify({"entity": saved, "removed": removed})


@bp.get("/entities/<entity_id>/outfit-sprites")
@login_required
def outfit_sprites(entity_id: str):
    """For a combined-format (sprite) character, return the composed
    sprite URL + stats for each of its outfits. Read-only: composes each
    outfit's worn state and builds the /sprites/... URL the compositor
    serves. Non-sprite characters get sprite_id=null and just the outfit
    stats (no composed image)."""
    import copy as _copy
    from .. import clothing_v2, sprite_url
    e = ent.get(entity_id)
    if not e or e.get("type") != "character":
        return jsonify({"error": "character not found"}), 404
    props = e.get("properties") or {}
    sid = sprite_url.sprite_id_of(e)
    entities = ent.load_all()
    scene = sprite_url.resolve_scene_tag(room=None, location=None, character=e)
    ordered = list(props.get("outfits") or [])
    cur = props.get("current_outfit")
    if cur and cur not in ordered:
        ordered.append(cur)
    out = []
    for oid in ordered:
        outfit = entities.get(oid)
        if not isinstance(outfit, dict) or outfit.get("type") != "outfit":
            continue
        oprops = outfit.get("properties") or {}
        equips = oprops.get("equips") or {}
        url = None
        if sid:
            ch = _copy.deepcopy(e)
            clothing_v2.apply_outfit_preset_v2(ch, outfit, entities)
            slot_tuple, garment_tuple = clothing_v2.resolve_sprite_slots_v2(ch, entities)
            slots = dict(zip(clothing_v2.SPRITE_SLOT_ORDER, slot_tuple))
            garments = dict(zip(clothing_v2.SPRITE_SLOT_ORDER, garment_tuple))
            url = sprite_url.build_url(
                host=_sprite_host(), sprite_id=sid,
                clothing_slots=slots, scene_tag=scene, garments=garments,
            )
        # Per-piece detail so the tab can render state pickers + the piece
        # json and recompose the sprite live as states change.
        pieces = []
        for slot, pid in equips.items():
            piece = entities.get(pid) or {}
            pprops = piece.get("properties") or {}
            pieces.append({
                "slot": slot,
                "piece_id": pid,
                "name": piece.get("name") or pid,
                "states": pprops.get("states") or ["on", "off"],
                "garment": pprops.get("garment") or "default",
                "sprite_slot": slot in clothing_v2.SPRITE_SLOT_ORDER,
                # Full editable fields — the studio saves this object back,
                # so dropping description/tags/children/example_text here
                # would blank them on the piece.
                "piece": {"id": piece.get("id") or pid, "type": "clothing",
                          "name": piece.get("name") or pid,
                          "description": piece.get("description") or "",
                          "tags": piece.get("tags") or [],
                          "example_text": piece.get("example_text") or "",
                          "children": piece.get("children") or [],
                          "properties": pprops} if piece else None,
            })
        out.append({
            "outfit_id": oid,
            "name": outfit.get("name") or oid,
            "url": url,
            "equips": equips,
            "pieces": pieces,
            "signature": oprops.get("signature_description") or "",
            "is_current": oid == cur,
        })
    return jsonify({"sprite_id": sid, "scene": scene, "outfits": out})


@bp.post("/entities/<entity_id>/compose-url")
@login_required
def compose_url(entity_id: str):
    """Build a composed sprite URL for a character wearing an arbitrary
    worn map — the live-preview companion to /outfit-sprites, called as
    the Images tab's per-piece state pickers change. JSON body:
    {worn: {slot: {piece, state}}}. Returns {url} (null if not a sprite
    character)."""
    import copy as _copy
    from .. import clothing_v2, sprite_url
    e = ent.get(entity_id)
    if not e or e.get("type") != "character":
        return jsonify({"error": "character not found"}), 404
    sid = sprite_url.sprite_id_of(e)
    if not sid:
        return jsonify({"url": None})
    payload = request.get_json(silent=True) or {}
    worn = payload.get("worn")
    if not isinstance(worn, dict):
        return jsonify({"error": "worn map required"}), 400
    entities = ent.load_all()
    ch = _copy.deepcopy(e)
    ch.setdefault("properties", {})["worn"] = worn
    slot_tuple, garment_tuple = clothing_v2.resolve_sprite_slots_v2(ch, entities)
    slots = dict(zip(clothing_v2.SPRITE_SLOT_ORDER, slot_tuple))
    garments = dict(zip(clothing_v2.SPRITE_SLOT_ORDER, garment_tuple))
    scene = sprite_url.resolve_scene_tag(room=None, location=None, character=e)
    url = sprite_url.build_url(
        host=_sprite_host(), sprite_id=sid,
        clothing_slots=slots, scene_tag=scene, garments=garments,
    )
    return jsonify({"url": url})


@bp.post("/entities/<entity_id>/outfit-from-images")
@login_required
def outfit_from_images(entity_id: str):
    """Create a new outfit from an uploaded image set.

    A composed image is layer-based (sprite wardrobe assets), so a flat
    uploaded set can't feed the /sprites compositor. Instead this makes
    the set an image-backed outfit: it saves the files, creates an image
    pack tagged with a unique tag, creates an outfit entity carrying that
    same tag, and registers the outfit on the character. Wearing the
    outfit puts its tag into the scene, which exposes the pack — so the
    picker draws from that image set while the outfit is worn.

    Multipart: name (required), files (one or more image files)."""
    from pathlib import Path
    import re
    e = ent.get(entity_id)
    if not e or e.get("type") != "character":
        return jsonify({"error": "character not found"}), 404
    name = (request.form.get("name") or "").strip()
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not name:
        return jsonify({"error": "name required"}), 400
    if not files:
        return jsonify({"error": "at least one image required"}), 400

    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "outfit"
    props = e.setdefault("properties", {})
    existing = set(props.get("outfits") or []) | set((props.get("image_packs") or {}).keys())
    oid = f"{entity_id}_{slug}"
    i = 1
    while oid in existing or ent.get(oid):
        oid = f"{entity_id}_{slug}_{i}"
        i += 1
    tag = oid

    folder = Path(current_app.config["data_dir"]) / "characters" / entity_id / "images"
    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for f in files:
        if Path(f.filename).suffix.lower() not in _IMAGE_EXTS:
            continue
        fn = _safe_image_name(folder, f.filename)
        f.save(folder / fn)
        entries.append({"caption": "", "image_url": f"/character_images/{entity_id}/{fn}"})
    if not entries:
        return jsonify({"error": "no images with a supported extension"}), 400

    props.setdefault("image_packs", {})[oid] = {
        "name": name, "default_enabled": False,
        "expose_tags": [tag], "entries": entries,
    }
    outfits = props.setdefault("outfits", [])
    if oid not in outfits:
        outfits.append(oid)
    # If the character isn't a sprite character, make sure it's on the
    # tagged path so the pack is consulted by the picker.
    images = props.setdefault("images", {})
    if not images.get("sprite_id") and (images.get("format") or "") != "combined":
        images.setdefault("format", "tagged")

    outfit = {
        "id": oid, "type": "outfit", "name": name, "description": "",
        "tags": [tag], "example_text": "", "children": [],
        "properties": {"owner": entity_id, "equips": {}, "signature_description": ""},
    }
    ent.save(outfit)
    saved = ent.save(e)
    return jsonify({"entity": saved, "outfit_id": oid, "images_added": len(entries)})


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


@bp.get("/conversations")
@login_required
def list_conversations():
    return jsonify({"conversations": convs.list_conversations()})


@bp.post("/conversations")
@login_required
def create_conversation():
    payload = request.get_json(silent=True) or {}
    scenario_id = payload.get("scenario_id")
    if not scenario_id:
        return jsonify({"error": "scenario_id required"}), 400
    # Accept the new pre-creation form fields. `start_toggles` is a
    # {<toggle_id>: bool} dict (e.g. `{"magic_item": true}`).
    # `random_picks` is a {<key>: <entity_id>} dict that pins the random
    # roll to a specific id (e.g. `{"partner": "cosmo"}`); unspecified
    # keys roll uniformly from the scenario's pool.
    raw_toggles = payload.get("start_toggles") or {}
    raw_picks = payload.get("random_picks") or {}
    start_toggles = (
        {k: bool(v) for k, v in raw_toggles.items() if isinstance(k, str)}
        if isinstance(raw_toggles, dict) else {}
    )
    random_picks = (
        {k: v for k, v in raw_picks.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(raw_picks, dict) else {}
    )
    try:
        conv = convs.create_conversation_from_scenario(
            scenario_id,
            title=payload.get("title"),
            active_setup_id=payload.get("setup_id"),
            start_toggles=start_toggles,
            random_pick_overrides=random_picks,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(conv), 201


@bp.get("/conversations/<cid>")
@login_required
def get_conversation(cid: str):
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    return jsonify(conv)


@bp.delete("/conversations/<cid>")
@login_required
def delete_conversation(cid: str):
    convs.delete_conversation(cid)
    return jsonify({"deleted": True})


@bp.post("/conversations/<cid>/reset-scene")
@login_required
def reset_scene(cid: str):
    """Wipe all messages + the running summary and reseed root narrator +
    greetings from the conversation's scenario. Instance entities (per-
    conversation character/outfit edits) are preserved.

    Body may include {"setup_id": "..."} to make a specific setup the
    active root after reseed, plus the same `start_toggles` /
    `random_picks` shape POST /conversations accepts.
    """
    payload = request.get_json(silent=True) or {}
    raw_toggles = payload.get("start_toggles") or {}
    raw_picks = payload.get("random_picks") or {}
    start_toggles = (
        {k: bool(v) for k, v in raw_toggles.items() if isinstance(k, str)}
        if isinstance(raw_toggles, dict) else {}
    )
    random_picks = (
        {k: v for k, v in raw_picks.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(raw_picks, dict) else {}
    )
    try:
        conv = convs.reseed_from_scenario(
            cid,
            active_setup_id=payload.get("setup_id"),
            start_toggles=start_toggles,
            random_pick_overrides=random_picks,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(conv)


@bp.post("/conversations/<cid>/scenario-prep/reroll-partner")
@login_required
def scenario_prep_reroll_partner(cid: str):
    """Re-roll the random partner. Two paths depending on staging state:

      - **Pre-start** (active staging root has no children): swap the
        partner in place — update presence, instance scenario, and
        metadata.random_picks on the active root. No wipe, no reseed.
        This is the common path used while the user is still picking
        on the staging panel.

      - **Post-start** (active root has descendants): full reseed via
        reseed_from_scenario, same as before. Wipes message history.
        Client warns the user before calling.

    Optional body: ``{partner_id: <id>}`` pins to a specific pool
    member; otherwise we roll uniformly from
    ``scenario.random_character_pool``.
    """
    payload = request.get_json(silent=True) or {}
    pinned = payload.get("partner_id")
    overrides: dict[str, str] = {}
    if isinstance(pinned, str) and pinned:
        overrides["partner"] = pinned
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404

    # Find the active staging root.
    active_root = None
    for m in conv.get("messages", {}).values():
        meta = m.get("metadata") or {}
        if meta.get("setup_active") and meta.get("staging"):
            active_root = m
            break
    has_descendants = bool(active_root) and any(
        m.get("parent_id") == active_root["id"]
        for m in conv.get("messages", {}).values()
    )

    if active_root and not has_descendants:
        # Pre-start path: swap in place without a reseed.
        from .. import setups as su
        from .. import entities as ent
        scenario_id = conv.get("scenario_id")
        # Mutate the live scenario in instance dir so the new partner
        # joins scenario.characters[] (mirroring create + reseed).
        scenario = ent.load_instance_entity(cid, scenario_id) or {}
        # Roll the new partner. Overrides win; otherwise random from pool.
        picks = su.roll_random_picks(
            scenario,
            overrides=overrides,
            toggles={},  # toggles are stored on the root; not relevant here
        )
        new_partner = picks.get("partner")
        if not new_partner:
            return jsonify({"error": "scenario has no random_character_pool"}), 400
        # Stamp the new partner into the scenario so future loads / cast
        # widgets see it.
        chars = list(scenario.get("characters") or [])
        if new_partner not in chars:
            chars.append(new_partner)
        scenario["characters"] = chars
        ent.save_instance_entity(cid, scenario)
        # Update the active root's metadata + presence.
        meta = active_root.setdefault("metadata", {})
        old_picks = meta.get("random_picks") or {}
        old_partner = old_picks.get("partner")
        meta["random_picks"] = {**old_picks, "partner": new_partner}
        # Instance the new partner if we don't have them yet — pool
        # members aren't pre-instanced at conversation creation, so the
        # first time the user swaps to a different one we need to copy
        # the master template into the instance dir + pull their
        # outfit chain in. Old partner leaves the cast (instance file
        # goes away; their master template stays in the library so the
        # user can swap back via the dropdown later).
        from .. import layers
        try:
            layers.add_to_conversation_cast(cid, new_partner)
        except ValueError:
            pass
        if old_partner and old_partner != new_partner:
            try:
                layers.remove_from_conversation_cast(cid, old_partner)
            except Exception:
                pass
        # Re-apply the setup state with the new partner. We need to
        # substitute macros against the new partner and rebuild the
        # presence_snapshot from the scenario's starting_state baseline
        # plus the (re-substituted) state edits.
        from ..setups import setup_list, substitute_macros, parse_setup_state
        setups = setup_list(scenario)
        setup = next(
            (s for s in setups if s["id"] == (meta.get("setup") or {}).get("id")),
            None,
        )
        if setup:
            new_partner_name = (ent.load_instance_entity(cid, new_partner) or {}).get("name") or new_partner
            new_state = substitute_macros(
                setup.get("state") or "",
                partner_id=new_partner,
                partner_name=new_partner_name,
            )
            user_edits, entity_edits = parse_setup_state(new_state)
            from ..conversations import (
                _baseline_presence_from_scenario, _apply_narrator_edits,
                _merge_presence_patch,
            )
            instance_ents = ent.load_instance_entities(cid) or {}
            baseline = _baseline_presence_from_scenario(scenario, instance_ents)
            # Drop the OLD partner from presence so they don't linger
            # in the scene next to the newly-picked one.
            if old_partner:
                pres = dict(baseline.get("presence") or {})
                pres.pop(old_partner, None)
                baseline = {**baseline, "presence": pres}
            presence_patch, applied_log = _apply_narrator_edits(
                cid, entity_edits + user_edits,
                {"presence": dict(baseline.get("presence") or {})},
            )
            snap = _merge_presence_patch(baseline, presence_patch)
            active_root["presence_snapshot"] = snap
            meta["applied_edits"] = applied_log

        # Propagate the new partner across EVERY staging root via the
        # shared helper, so the date setup's [Scenario] / opening
        # prompt / sidebar all reflect the swap (not just the active
        # roommate setup).
        convs.propagate_partner_to_staging_roots(
            conv, scenario, new_partner, new_partner_name,
        )

        convs.save_conversation(conv)
        return jsonify({"partner": new_partner, "wiped": False})

    # Post-start path: full reseed (wipes messages).
    active_setup_id = None
    start_toggles_carry: dict[str, bool] = {}
    for m in conv.get("messages", {}).values():
        meta = m.get("metadata") or {}
        if meta.get("setup_active"):
            active_setup_id = (meta.get("setup") or {}).get("id")
            start_toggles_carry = dict(meta.get("start_toggles") or {})
            break
    try:
        fresh = convs.reseed_from_scenario(
            cid,
            active_setup_id=active_setup_id,
            start_toggles=start_toggles_carry,
            random_pick_overrides=overrides,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(fresh)


@bp.post("/conversations/<cid>/scenario-prep/start")
@login_required
def scenario_prep_start(cid: str):
    """Resolve the active staging root into the actual scene — appends
    the opening narrator prose as a child of the staging root and
    hangs the per-character first_message greetings off it. The prep
    panel hides itself once children exist.

    Optional body: ``{setup_id: <id>}`` to start a non-active staging
    root. Defaults to whichever root carries setup_active.
    """
    payload = request.get_json(silent=True) or {}
    setup_id = payload.get("setup_id") if isinstance(payload, dict) else None
    try:
        fresh = convs.start_staging(cid, setup_id=setup_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(fresh)


@bp.post("/conversations/<cid>/scenario-prep/roll-item")
@login_required
def scenario_prep_roll_item(cid: str):
    """Pick a random item from the scenario's ``random_item_pool`` (or
    the id supplied in the body) and add it to the conversation cast
    via the existing add_to_conversation_cast path. Doesn't touch
    messages — the item just appears in the cast list and the narrator
    can surface it from the next turn onwards.

    Body: ``{item_id?: str}`` — optional pin.
    """
    import random
    from .. import layers
    from ..setups import random_item_pool
    payload = request.get_json(silent=True) or {}
    pinned = payload.get("item_id") if isinstance(payload, dict) else None

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    scenario_id = conv.get("scenario_id")
    scen = ent.get(scenario_id) if scenario_id else None
    if not scen:
        return jsonify({"error": "scenario not found"}), 400
    pool = random_item_pool(scen)
    if not pool:
        return jsonify({"error": "scenario has no random_item_pool"}), 400

    if isinstance(pinned, str) and pinned:
        if pinned not in pool:
            return jsonify({"error": f"{pinned!r} not in random_item_pool"}), 400
        item_id = pinned
    else:
        # Prefer items not already in cast, so multiple rolls cycle
        # through the pool instead of landing on the same one.
        already = {
            eid for eid in (
                ent.load_instance_entities(cid) or {}
            )
            if (ent.load_instance_entity(cid, eid) or {}).get("type") == "object"
        }
        candidates = [p for p in pool if p not in already] or pool
        item_id = random.choice(candidates)

    try:
        layers.add_to_conversation_cast(cid, item_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"item_id": item_id})


@bp.post("/conversations/<cid>/scenario-prep/scene-stage")
@login_required
def scenario_prep_scene_stage(cid: str):
    """Spawn a child branch under a Scene staging root using the user's
    picks. Each call creates a fresh sibling chain off the same root,
    so the user can re-stage by clicking Start again — the new branch
    is selected as active and shows up next to prior stagings via the
    existing branch chips on the staging root.

    Body: ``{setup_id, characters[], outfits{}, location, room, prompt}``.
    """
    payload = request.get_json(silent=True) or {}
    setup_id = payload.get("setup_id") if isinstance(payload, dict) else None
    if not isinstance(setup_id, str) or not setup_id:
        return jsonify({"error": "setup_id required"}), 400
    # user_persona is an optional {name, description} object — comes
    # from the panel's preset dropdown (or its Custom… free-text
    # fields). When present, start_scene_staging emits [set user.name
    # = …] / [set user.description = …] edits on the new root so the
    # branch's effective persona path-replays correctly.
    # Two shapes accepted, gated on scenario.user_personas_are_roles:
    #   legacy  → {name, description}    → patches user.name / user.description
    #   role    → {role, role_description} → patches user.role / role_description
    # The panel sends one or the other based on the scenario flag; the
    # backend just forwards the keys to start_scene_staging which
    # materializes each as a patch via _nest_value.
    raw_persona = payload.get("user_persona") if isinstance(payload, dict) else None
    user_persona = None
    if isinstance(raw_persona, dict):
        role_label = raw_persona.get("role")
        role_desc = raw_persona.get("role_description")
        name = raw_persona.get("name")
        desc = raw_persona.get("description")
        if isinstance(role_label, str) and role_label.strip():
            user_persona = {
                "role": role_label.strip(),
                "role_description": role_desc.strip() if isinstance(role_desc, str) else "",
            }
        elif isinstance(name, str) and name.strip():
            user_persona = {
                "name": name.strip(),
                "description": desc.strip() if isinstance(desc, str) else "",
            }
    picks = {
        "characters": payload.get("characters"),
        "outfits": payload.get("outfits"),
        "slot_states": payload.get("slot_states"),
        "location": payload.get("location"),
        "room": payload.get("room"),
        # Optional per-character placement maps {char_id: location/room id}.
        # Override the batch location/room above on a per-NPC basis; absent or
        # empty entries fall back to the batch default.
        "cast_locations": payload.get("cast_locations"),
        "cast_rooms": payload.get("cast_rooms"),
        "prompt": payload.get("prompt"),
        "user_persona": user_persona,
        # Per-instance overrides for location/room descriptions. Each
        # is a {entity_id: new_description} map; start_scene_staging
        # emits a [patch] edit per non-empty entry so path-replay picks
        # up the override when the prompt loads loc/room descriptions.
        "location_descriptions": payload.get("location_descriptions"),
        "room_descriptions": payload.get("room_descriptions"),
    }

    def _str(key: str) -> str:
        v = payload.get(key)
        return v.strip() if isinstance(v, str) else ""

    narrator_directive = _str("narrator_edits")
    location_directive = _str("location_prompt")
    scenario_instructions = payload.get("scenario_instructions")
    if not isinstance(scenario_instructions, str):
        scenario_instructions = None
    setup_append = payload.get("setup_append")
    if not isinstance(setup_append, str):
        setup_append = None

    extra_edits: list = []
    body_override: str | None = None
    narrator_raw: str | None = None
    location_raw: str | None = None

    # ------------------------------------------------------------------
    # New rooms created via the staging panel's "+ Add custom room"
    # form. Each entry is {tmp_id, name, description, location_id};
    # we mint a real entity id, write the room file into the
    # conversation's instance dir, and remap any tmp_id referenced
    # by picks.room / payload.user_room to the real id before the
    # [move] edits get emitted by start_scene_staging.
    #
    # Conversation-scoped: future stagings won't see the new room in
    # the dropdown (the staging-options endpoint reads templates only),
    # but path-replay finds the room because it lives in the instance
    # entity dir.
    # ------------------------------------------------------------------
    raw_new_rooms = payload.get("new_rooms") if isinstance(payload, dict) else None
    tmp_to_real: dict[str, str] = {}
    if isinstance(raw_new_rooms, list) and raw_new_rooms:
        all_ents_for_loc = ent.load_all()
        for entry in raw_new_rooms:
            if not isinstance(entry, dict):
                continue
            tmp_id = entry.get("tmp_id")
            name = entry.get("name")
            desc = entry.get("description") or ""
            loc_id = entry.get("location_id")
            if not (isinstance(tmp_id, str) and tmp_id):
                continue
            if not (isinstance(name, str) and name.strip()):
                continue
            if not (isinstance(loc_id, str) and loc_id):
                continue
            if not isinstance(desc, str):
                desc = ""
            # Confirm the parent location is real before we create a
            # room anchored to it; skip silently if the user picked an
            # id that no longer resolves (e.g., the scenario shipped
            # an unknown id in scene_staging_fields.locations).
            parent_loc = all_ents_for_loc.get(loc_id)
            if not parent_loc or parent_loc.get("type") != "location":
                continue
            real_id = f"room_{ent.new_id()[:12]}"
            room_entity = {
                "id": real_id,
                "type": "room",
                "name": name.strip(),
                "description": desc,
                "tags": [],
                "example_text": "",
                "children": [],
                "properties": {
                    "_custom_for_location": loc_id,
                },
            }
            ent.save_instance_entity(cid, room_entity)
            tmp_to_real[tmp_id] = real_id
            # Also splice into the parent location's instance entity so
            # the children list stays internally consistent — useful for
            # any downstream code that walks loc.children.
            inst_loc = ent.load_instance_entity(cid, loc_id)
            if inst_loc is not None:
                children = list(inst_loc.get("children") or [])
                if real_id not in children:
                    children.append(real_id)
                    inst_loc["children"] = children
                    ent.save_instance_entity(cid, inst_loc)

    # Remap tmp room id references from the picks dict so downstream
    # code (start_scene_staging, the user_location remap below) sees
    # only real entity ids.
    if tmp_to_real:
        if isinstance(picks.get("room"), str) and picks["room"] in tmp_to_real:
            picks["room"] = tmp_to_real[picks["room"]]
        raw_user_room = payload.get("user_room") if isinstance(payload, dict) else None
        if isinstance(raw_user_room, str) and raw_user_room in tmp_to_real:
            # Patched into the payload dict so the existing user-room
            # code path picks up the resolved id when it reads
            # payload["user_room"] below.
            payload["user_room"] = tmp_to_real[raw_user_room]
        # Same remap for any description-override map entries keyed
        # by the tmp_id (the JS forwards override edits made after
        # the new-room entry is staged; we route them to the real id).
        for picks_key in ("room_descriptions",):
            raw_map = picks.get(picks_key) or {}
            if not isinstance(raw_map, dict):
                continue
            remapped = {}
            for k, v in raw_map.items():
                if isinstance(k, str) and k in tmp_to_real:
                    remapped[tmp_to_real[k]] = v
                else:
                    remapped[k] = v
            picks[picks_key] = remapped

    # User Character pick — swap the conversation's user entity to
    # the chosen card template before any other edits land. Mirrors
    # the /user-persona endpoint: deep-copy the template into the
    # existing `user` instance, preserving id="user" so narrator
    # directives + path-replay still target the same entity.
    raw_user_card_id = payload.get("user_card_id") if isinstance(payload, dict) else None
    if isinstance(raw_user_card_id, str) and raw_user_card_id.strip():
        from ..entities import (
            get as _ent_get,
            load_instance_entity as _load_inst,
            save_instance_entity as _save_inst,
            replace_entity as _replace_ent,
        )
        template = _ent_get(raw_user_card_id.strip())
        if (template
            and template.get("type") == "character"
            and "user" in (template.get("tags") or [])):
            user_entity = copy.deepcopy(template)
            user_entity["id"] = "user"
            user_entity["type"] = "character"
            user_entity["_template_id"] = raw_user_card_id.strip()
            tags = list(user_entity.get("tags") or [])
            if "user" not in tags:
                tags.append("user")
            user_entity["tags"] = tags
            try:
                if _load_inst(cid, "user") is not None:
                    _replace_ent(cid, user_entity)
                else:
                    _save_inst(cid, user_entity)
                # Mirror the chosen card_id into settings.user_persona
                # so the left-panel card picker shows it selected and
                # subsequent staging-options requests fetch the right
                # wardrobe pool.
                conv_for_card = convs.load_conversation(cid)
                if conv_for_card is not None:
                    settings_pers = (
                        conv_for_card.setdefault("settings", {})
                        .setdefault("user_persona", {})
                    )
                    settings_pers["card_id"] = raw_user_card_id.strip()
                    convs.save_conversation(conv_for_card)
            except Exception:
                current_app.logger.exception(
                    "user_card_id swap failed cid=%s card=%s", cid, raw_user_card_id
                )

    # User Location pick — independent of the cast's room. Empty
    # means "with the cast" — user moves to the same location/room
    # the cast is being moved to. Non-empty emits [move user ->
    # loc:room] so the user starts in their own place.
    raw_user_loc = payload.get("user_location") if isinstance(payload, dict) else None
    raw_user_room = payload.get("user_room") if isinstance(payload, dict) else None
    if (isinstance(raw_user_loc, str) and raw_user_loc.strip()
        and isinstance(raw_user_room, str) and raw_user_room.strip()):
        extra_edits.append({
            "kind": "move",
            "character_id": "user",
            "location": raw_user_loc.strip(),
            "room": raw_user_room.strip(),
        })
    else:
        # "With the cast" — copy the cast's destination onto the user
        # so they actually end up in the same room. Without this branch
        # the user is left at whatever location the parent staging
        # root carried (often the scenario's starting_state or
        # nothing), which is the failure mode the dropdown defaults
        # to. start_scene_staging only emits move edits for picked
        # characters; the user is never in that list.
        cast_loc = payload.get("location") if isinstance(payload, dict) else None
        cast_room = payload.get("room") if isinstance(payload, dict) else None
        if (isinstance(cast_loc, str) and cast_loc.strip()
            and isinstance(cast_room, str) and cast_room.strip()):
            extra_edits.append({
                "kind": "move",
                "character_id": "user",
                "location": cast_loc.strip(),
                "room": cast_room.strip(),
            })

    # Both narrator field and location-prompt field route through the
    # same narrator model entry-point. Difference is in what we keep:
    # narrator_edits → keep edits, discard rewritten body (it modifies
    # character defs, doesn't generate the opening prose). Location
    # prompt → keep BOTH the body (becomes the opening narrator prose)
    # and any edits the model also emits along the way (they're free
    # context the user can use to set up the scene).
    needs_model = bool(narrator_directive or location_directive)
    if needs_model:
        from ..narrator_add import narrator_add_message_sync

        conv = convs.load_conversation(cid)
        if not conv:
            return jsonify({"error": "conversation not found"}), 404
        settings = conv.get("settings") or {}
        model = settings.get("ollama_model_override") or (
            current_app.config.get("ollama") or {}
        ).get("model")
        profile = (current_app.config.get("model_profiles") or {}).get(model) or {}
        conv_sampling = settings.get("sampling") or {}
        sampling = {**profile, **conv_sampling}
        enable_thinking = bool(settings.get("enable_thinking", False))

        def _run_narrator(directive: str, target_body: str) -> dict:
            synth_id = "_scene_staging_synth_target"
            conv["messages"][synth_id] = {
                "id": synth_id,
                "parent_id": None,
                "persona": "narrator",
                "speaker_id": None,
                "content": target_body or "",
                "presence_snapshot": {},
                "created_at": int(__import__("time").time()),
                "edited_at": None,
                "metadata": {},
            }
            try:
                return narrator_add_message_sync(
                    conv, synth_id, directive,
                    model=model, options=sampling, think=enable_thinking,
                )
            finally:
                conv["messages"].pop(synth_id, None)

        if narrator_directive:
            try:
                r = _run_narrator(narrator_directive, "")
            except Exception as e:
                return jsonify({"error": f"narrator call failed: {e}"}), 500
            extra_edits.extend(r.get("edits") or [])
            narrator_raw = r.get("raw_response")

        if location_directive:
            # Seed the synth body with the user's typed prompt (if any)
            # so the model has a starting point to rewrite — same
            # pattern the existing narrator-edit endpoint relies on
            # ("rewrite this body" beats "generate from nothing").
            target_body = picks.get("prompt") or ""
            if not isinstance(target_body, str):
                target_body = ""
            try:
                r = _run_narrator(location_directive, target_body)
            except Exception as e:
                return jsonify({"error": f"location prompt failed: {e}"}), 500
            extra_edits.extend(r.get("edits") or [])
            new_body = (r.get("new_body") or "").strip()
            if new_body:
                body_override = new_body
            location_raw = r.get("raw_response")

    # Modules + per-module settings from the staging panel. Validate
    # against the scenario's available_modules so a client can't enable
    # something the scenario didn't opt into. Settings are coerced
    # through each manifest's schema (defaulting any missing keys).
    from .. import modules as _modules_mod
    from ..entities import get as _entity_get
    raw_module_ids = payload.get("modules") if isinstance(payload, dict) else None
    raw_module_settings = payload.get("module_settings") if isinstance(payload, dict) else None
    if not isinstance(raw_module_settings, dict):
        raw_module_settings = {}
    conv_for_scn = convs.load_conversation(cid)
    scn_id = (conv_for_scn or {}).get("scenario_id") or ""
    scenario_obj = _entity_get(scn_id) if scn_id else None
    available_module_ids = []
    if isinstance(scenario_obj, dict):
        raw_avail = scenario_obj.get("available_modules")
        if isinstance(raw_avail, list):
            available_module_ids = [m for m in raw_avail if isinstance(m, str)]
    validated_modules = _modules_mod.filter_active(raw_module_ids, available_module_ids)
    validated_settings: dict[str, dict] = {}
    for mid in validated_modules:
        manifest = _modules_mod.get(mid)
        if not manifest:
            continue
        validated_settings[mid] = _modules_mod.coerce_settings(
            manifest, raw_module_settings.get(mid),
        )

    # Prefabs: dispatch the staging-panel picks for each opted-in
    # prefab to a kind-specific handler. v2 payload shape:
    #   payload.prefabs = { <prefab_id>: { ...kind-specific... } }
    # Legacy callers that only know about the objects prefab still
    # send `objects` / `equipped` at the top level; the dispatcher
    # below tolerates either shape so the JS rollout can lag.
    from .. import prefabs as _prefabs_mod
    raw_prefabs_block = payload.get("prefabs") if isinstance(payload, dict) else None
    if not isinstance(raw_prefabs_block, dict):
        raw_prefabs_block = {}

    picked_chars_set = set(c for c in (picks.get("characters") or []) if isinstance(c, str))
    picked_chars_set.add("user")

    available_prefab_ids = []
    if isinstance(scenario_obj, dict):
        available_prefab_ids = [
            m.get("id")
            for m in _prefabs_mod.list_for_scenario(scenario_obj)
            if isinstance(m.get("id"), str)
        ]

    pf_ctx = _prefabs_mod.PrefabContext(
        scenario=scenario_obj if isinstance(scenario_obj, dict) else {},
        cid=cid,
        picks=picks if isinstance(picks, dict) else {},
        payload=payload if isinstance(payload, dict) else {},
        picked_chars=picked_chars_set,
    )
    # Generic dispatch: each opted-in prefab is handled by the kind
    # registered for its `staging_ui.kind` (builtins + drop-ins alike).
    # The route never names a kind — see app/prefabs/registry.py.
    for prefab_id in available_prefab_ids:
        manifest = _prefabs_mod.get(prefab_id)
        if not manifest:
            continue
        handler = _prefabs_mod.get_kind(_prefabs_mod.staging_kind_of(manifest))
        if handler is None:
            continue
        ui = _prefabs_mod.staging_ui_of(manifest)
        cfg = (
            _prefabs_mod.scenario_config(scenario_obj, prefab_id)
            if isinstance(scenario_obj, dict) else {}
        )
        pf_payload = raw_prefabs_block.get(prefab_id)
        if not isinstance(pf_payload, dict):
            pf_payload = {}
        for edit in (handler.apply_picks(manifest, ui, cfg, pf_payload, pf_ctx) or []):
            if isinstance(edit, dict):
                extra_edits.append(edit)

    # Life Sim stats removals: per character, a list of stat ids the
    # user dropped via the chip × on the staging panel. Emitted as
    # unset edits BEFORE the stats_edits patches so re-adding a stat
    # with the same id ends up with the patch's value (last edit
    # wins). Path-replay applies these in declared order.
    #
    # Both stats_edits and stats_removed are gated on life_sim being
    # checked in the modules picker — when the module is off, the
    # client wouldn't render the stats UI and we don't process the
    # payload here either. "Modules off should change nothing."
    life_sim_active = "life_sim" in validated_modules
    raw_stats_removed = payload.get("stats_removed") if (isinstance(payload, dict) and life_sim_active) else None
    if isinstance(raw_stats_removed, dict):
        for char_id, removed in raw_stats_removed.items():
            if char_id not in picked_chars_set or not isinstance(removed, list):
                continue
            for sid in removed:
                if not isinstance(sid, str) or not sid:
                    continue
                norm_id = "".join(
                    c if (c.isalnum() or c == "_") else "_"
                    for c in sid.strip().lower()
                ).strip("_")
                if not norm_id:
                    continue
                extra_edits.append({
                    "kind": "unset",
                    "id": char_id,
                    "path": ["properties", "stats", norm_id],
                })

    # Life Sim stats edits from the staging panel: per character, a
    # dict of stat_id -> partial schema (just .value for an existing
    # stat, full {value, label, min, max} for a brand-new one the user
    # added via the "+ stat" form). One patch per character; deep_merge
    # keeps the rest of the stats dict intact.
    raw_stats_edits = payload.get("stats_edits") if (isinstance(payload, dict) and life_sim_active) else None
    if isinstance(raw_stats_edits, dict):
        for char_id, char_edits in raw_stats_edits.items():
            if char_id not in picked_chars_set or not isinstance(char_edits, dict):
                continue
            cleaned: dict[str, dict] = {}
            for sid, body in char_edits.items():
                if not isinstance(sid, str) or not sid or not isinstance(body, dict):
                    continue
                # Sanitize the id to match the snake_case convention.
                norm_id = "".join(
                    c if (c.isalnum() or c == "_") else "_"
                    for c in sid.strip().lower()
                ).strip("_")
                if not norm_id:
                    continue
                row: dict = {}
                if "value" in body:
                    try:
                        row["value"] = int(body["value"])
                    except (TypeError, ValueError):
                        pass
                if "label" in body and isinstance(body["label"], str):
                    row["label"] = body["label"].strip()
                if "min" in body:
                    try:
                        row["min"] = int(body["min"])
                    except (TypeError, ValueError):
                        pass
                if "max" in body:
                    try:
                        row["max"] = int(body["max"])
                    except (TypeError, ValueError):
                        pass
                if row:
                    cleaned[norm_id] = row
            if cleaned:
                extra_edits.append({
                    "kind": "patch",
                    "id": char_id,
                    "data": {"properties": {"stats": cleaned}},
                })

    try:
        fresh = convs.start_scene_staging(
            cid, setup_id=setup_id, picks=picks,
            extra_edits=extra_edits,
            body_override=body_override,
            narrator_directive=narrator_directive or None,
            narrator_raw_response=narrator_raw,
            location_directive=location_directive or None,
            location_raw_response=location_raw,
            scenario_instructions_base=scenario_instructions,
            scenario_instructions_append=setup_append,
            modules=validated_modules,
            module_settings=validated_settings,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(fresh)


@bp.get("/scenarios/<sid>/scene-staging/<setup_id>/options")
@login_required
def scene_staging_options(sid: str, setup_id: str):
    """Return the option pools the Scene staging panel renders for one
    setup. Pulls characters + outfits + locations + rooms from the
    master entity catalog so the panel doesn't have to make N calls.
    """
    from .. import entities as ent
    from ..setups import setup_list, scene_staging_fields, outfits_for

    scen = ent.get(sid)
    if not scen or scen.get("type") != "scenario":
        return jsonify({"error": "scenario not found"}), 404
    setup = next(
        (s for s in setup_list(scen) if s["id"] == setup_id),
        None,
    )
    if not setup:
        return jsonify({"error": "setup not found"}), 404
    fields = scene_staging_fields(setup)
    if not fields:
        return jsonify({"error": "setup is not a scene staging setup"}), 400

    all_ents = ent.load_all()

    char_ids = [c for c in (fields.get("characters") or []) if isinstance(c, str)]
    characters = []
    for cid in char_ids:
        e = all_ents.get(cid)
        if not e or e.get("type") != "character":
            continue
        cur = (e.get("properties") or {}).get("current_outfit") or ""
        # Per-character default slot map: read the current outfit's
        # clothing_slots so the panel can render one button per slot
        # the outfit actually occupies. Slots default to 1 (on); we
        # only emit slot buttons for slots present in the outfit.
        cur_slots: dict[str, int] = {}
        cur_outfit_ent = all_ents.get(cur) if cur else None
        if cur_outfit_ent:
            raw = (cur_outfit_ent.get("properties") or {}).get("clothing_slots") or {}
            if isinstance(raw, dict):
                for slot_name, val in raw.items():
                    if not isinstance(slot_name, str):
                        continue
                    try:
                        n = int(val)
                    except (TypeError, ValueError):
                        continue
                    if n in (1, 2, 3):
                        cur_slots[slot_name.lower()] = n
        # Life Sim stats: surface the declared schema per character so
        # the staging panel can render an editable starting-value
        # input per stat (and an "Add stat" form for branch-only stats
        # that don't exist on the character template). Empty when the
        # character has no stats — the panel hides the section.
        char_stats: list[dict] = []
        raw_stats = (e.get("properties") or {}).get("stats") or {}
        if isinstance(raw_stats, dict):
            for sid, body in raw_stats.items():
                if not isinstance(sid, str) or not isinstance(body, dict):
                    continue
                try:
                    val = int(body.get("value", 0))
                except (TypeError, ValueError):
                    val = 0
                try:
                    lo = int(body.get("min", 0))
                except (TypeError, ValueError):
                    lo = 0
                try:
                    hi = int(body.get("max", 100))
                except (TypeError, ValueError):
                    hi = 100
                char_stats.append({
                    "id": sid,
                    "label": (body.get("label") or sid),
                    "value": val,
                    "min": lo,
                    "max": hi,
                })
        characters.append({
            "id": cid,
            "name": e.get("name") or cid,
            "current_outfit": cur,
            "current_slots": cur_slots,
            "outfits": outfits_for(cid, all_ents),
            "stats": char_stats,
        })

    loc_ids = [l for l in (fields.get("locations") or []) if isinstance(l, str)]
    locations = []
    for lid in loc_ids:
        e = all_ents.get(lid)
        if not e or e.get("type") != "location":
            continue
        rooms = []
        for rid in e.get("children") or []:
            re = all_ents.get(rid)
            if not re or re.get("type") != "room":
                continue
            rooms.append({
                "id": rid,
                "name": re.get("name") or rid,
                "description": re.get("description") or "",
            })
        locations.append({
            "id": lid,
            "name": e.get("name") or lid,
            "description": e.get("description") or "",
            "rooms": rooms,
        })

    # Optional persona presets — the scenario author lists role
    # presets ({id, name, description}) the user can pick from on
    # the panel instead of typing name/description from scratch.
    # Canonical location is scenario-level `user_personas`; falls back
    # to the legacy scene_staging_fields.user_personas for scenarios
    # that haven't been migrated yet.
    raw_personas = scen.get("user_personas") or fields.get("user_personas") or []
    user_personas = []
    if isinstance(raw_personas, list):
        for p in raw_personas:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            name = p.get("name")
            if not isinstance(pid, str) or not isinstance(name, str):
                continue
            user_personas.append({
                "id": pid,
                "name": name,
                # Optional dropdown-label override. When set, the
                # staging panel's preset dropdown shows `label` while
                # the chosen persona's stamped user.name is still
                # `name`. Useful when the dropdown needs a different
                # word than the AI will call the user (e.g., dropdown
                # "Stranger" → stamped name "Alex").
                "label": p.get("label") if isinstance(p.get("label"), str) else None,
                "description": p.get("description") if isinstance(p.get("description"), str) else "",
            })

    # Clothing the user can wear — character-agnostic. The staging
    # panel surfaces every outfit template the library knows about
    # so the user can dress themselves in anything regardless of
    # which Character card they've picked. Owner-tag becomes a
    # display hint, not a gate.
    user_outfits: list[dict] = []
    for ent_obj in all_ents.values():
        if not isinstance(ent_obj, dict) or ent_obj.get("type") != "outfit":
            continue
        oid = ent_obj.get("id")
        if not oid:
            continue
        owner = ((ent_obj.get("properties") or {}).get("owner") or "").strip().lower()
        slots_raw = (ent_obj.get("properties") or {}).get("clothing_slots") or {}
        slots: dict[str, int] = {}
        if isinstance(slots_raw, dict):
            for k, v in slots_raw.items():
                if not isinstance(k, str):
                    continue
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    continue
                if n in (1, 2, 3):
                    slots[k.lower()] = n
        user_outfits.append({
            "id": oid,
            "name": ent_obj.get("name") or oid,
            "generic": owner in ("", "generic"),
            "owner": owner,
            "clothing_slots": slots,
            "partial_label": ((ent_obj.get("properties") or {}).get("partial_label") or None),
        })
    user_outfits.sort(key=lambda o: (not o["generic"], (o["name"] or "").lower()))

    # Character cards the user can BE — globally available characters
    # whose tags include "user". Surfaced as a "Character" dropdown so
    # the user picks their identity (Alex / Nadia / etc.) at staging
    # time. On Start the scene-stage route copies the chosen template
    # into the conversation's user entity.
    user_cards: list[dict] = []
    for ent_obj in all_ents.values():
        if not isinstance(ent_obj, dict) or ent_obj.get("type") != "character":
            continue
        tags = ent_obj.get("tags") or []
        if not isinstance(tags, list) or "user" not in tags:
            continue
        user_cards.append({
            "id": ent_obj.get("id"),
            "name": ent_obj.get("name") or ent_obj.get("id"),
            "description": (ent_obj.get("description") or "").strip(),
        })
    user_cards.sort(key=lambda c: (c["name"] or "").lower())

    # Modules: the staging panel renders a checkbox per scenario-
    # declared `available_modules` entry, pre-checks each id in the
    # setup's `default_modules`, and surfaces the per-module settings
    # form (auto-generated from each manifest's `settings` schema).
    # The panel POSTs back `modules: [...]` and `module_settings: {...}`
    # which the scene-stage route validates and stamps on the new
    # branch's root metadata.
    from .. import modules as _modules_mod
    module_manifests = _modules_mod.list_for_scenario(scen)
    default_module_ids = setup.get("default_modules") or []

    # Prefabs: similar shape to modules, but staging-time only — each
    # prefab contributes a section to the panel keyed off its
    # `properties.staging_ui.kind`. Dispatch each prefab to a
    # kind-specific resolver so the route stays open to new kinds
    # without growing more `if pid == ...` branches.
    from .. import prefabs as _prefabs_mod
    prefab_manifests = _prefabs_mod.list_for_scenario(scen)
    prefab_payload: dict[str, dict] = {}
    for manifest in prefab_manifests:
        pid = manifest.get("id")
        if not pid:
            continue
        cfg = _prefabs_mod.scenario_config(scen, pid)
        ui = _prefabs_mod.staging_ui_of(manifest)
        kind = _prefabs_mod.staging_kind_of(manifest)
        block: dict = {"manifest": manifest, "config": cfg, "kind": kind}
        # Generic dispatch: the kind handler contributes its panel
        # section (pool / ui metadata). No `if kind == ...` here.
        handler = _prefabs_mod.get_kind(kind)
        if handler is not None:
            pf_ctx = _prefabs_mod.PrefabContext(scenario=scen, catalog=all_ents)
            extra = handler.build_panel(manifest, ui, cfg, pf_ctx)
            if isinstance(extra, dict):
                block.update(extra)

        prefab_payload[pid] = block

    # Author-supplied pre-selections for the staging panel. Surfaced
    # under `defaults` on the per-setup `scene_staging_fields` block:
    #
    #   "defaults": {
    #     "characters": ["iris", "dex"],        # pre-checked cast
    #     "location": "the_marginalia",         # pre-selected location
    #     "room":     "marginalia_floor",       # pre-selected room
    #     "user_card_id": "generic_blonde_guy", # default Character card
    #     "user_outfit":  "alex_casual_v2",
    #     "outfits":  { "iris": "iris_apron_v2", ... }
    #   }
    #
    # All keys are optional; missing keys leave the panel blank in
    # those slots (legacy behavior). Unknown ids are filtered out so
    # an author typo can't crash the panel.
    raw_defaults = fields.get("defaults") if isinstance(fields, dict) else None
    defaults: dict = {}
    if isinstance(raw_defaults, dict):
        valid_char_ids = {c["id"] for c in characters}
        valid_loc_ids = {l["id"] for l in locations}
        valid_room_ids: set[str] = set()
        for loc in locations:
            for r in loc.get("rooms") or []:
                if r.get("id"):
                    valid_room_ids.add(r["id"])
        valid_card_ids = {c["id"] for c in user_cards}
        valid_outfit_ids = {o["id"] for o in user_outfits}
        # Filter each key against its pool of valid ids.
        dchars = raw_defaults.get("characters")
        if isinstance(dchars, list):
            defaults["characters"] = [
                c for c in dchars if isinstance(c, str) and c in valid_char_ids
            ]
        dloc = raw_defaults.get("location")
        if isinstance(dloc, str) and dloc in valid_loc_ids:
            defaults["location"] = dloc
        droom = raw_defaults.get("room")
        if isinstance(droom, str) and droom in valid_room_ids:
            defaults["room"] = droom
        dcard = raw_defaults.get("user_card_id")
        if isinstance(dcard, str) and dcard in valid_card_ids:
            defaults["user_card_id"] = dcard
        doutfit = raw_defaults.get("user_outfit")
        if isinstance(doutfit, str) and doutfit in valid_outfit_ids:
            defaults["user_outfit"] = doutfit
        dpersona = raw_defaults.get("user_persona")
        valid_persona_ids = {
            p.get("id") for p in user_personas if isinstance(p, dict)
        }
        if isinstance(dpersona, str) and dpersona in valid_persona_ids:
            defaults["user_persona"] = dpersona
        douts = raw_defaults.get("outfits")
        if isinstance(douts, dict):
            kept: dict[str, str] = {}
            for cid, oid in douts.items():
                if not isinstance(cid, str) or not isinstance(oid, str):
                    continue
                if cid not in valid_char_ids or oid not in valid_outfit_ids:
                    continue
                kept[cid] = oid
            if kept:
                defaults["outfits"] = kept

    return jsonify({
        "characters": characters,
        "locations": locations,
        # Scenario's base scenario_instructions — the panel pre-fills
        # the "Scenario instructions" textarea with this so the user
        # can edit-in-place rather than retyping from scratch.
        "scenario_instructions": (scen.get("scenario_instructions") or "").strip(),
        "user_personas": user_personas,
        "user_personas_are_roles": bool(scen.get("user_personas_are_roles")),
        "user_outfits": user_outfits,
        "user_cards": user_cards,
        "modules": module_manifests,
        "default_modules": [
            m for m in default_module_ids if isinstance(m, str)
        ],
        "prefabs": prefab_payload,
        "defaults": defaults,
    })


@bp.get("/modules")
@login_required
def list_modules():
    """Return every module manifest discovered under data/modules/.
    The chat UI uses this to render module controls outside of a
    Scene staging panel (the live left-panel section + the toolbar
    autoplay toggle). Frontend filters to active modules using the
    setup root's `metadata.modules`."""
    from .. import modules as _modules_mod
    manifests = list(_modules_mod.all_manifests().values())
    return jsonify({"modules": manifests})


@bp.put("/conversations/<cid>/active-setup/modules")
@login_required
def update_active_setup_modules(cid: str):
    """Update the active setup root's `metadata.modules` and
    `metadata.module_settings`. Used by the live toolbar autoplay
    toggle and the left-panel modules section.

    Body shape: ``{modules: [id], module_settings: {id: {key: value}}}``.
    Either field is optional; omitted fields are left untouched.
    Module ids and settings are validated against the scenario's
    `available_modules` allowlist + each manifest's schema, identical
    to the validation the scene-staging POST runs.
    """
    from .. import modules as _modules_mod
    from ..entities import get as _entity_get

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    leaf_id = conv.get("active_path_leaf") or ""
    root = active_setup_root_for_path(conv, leaf_id)
    if not root:
        return jsonify({"error": "no active setup root"}), 400

    payload = request.get_json(silent=True) or {}
    scn_id = conv.get("scenario_id") or ""
    scenario_obj = _entity_get(scn_id) if scn_id else None
    available_ids: list[str] = []
    if isinstance(scenario_obj, dict):
        raw_avail = scenario_obj.get("available_modules")
        if isinstance(raw_avail, list):
            available_ids = [m for m in raw_avail if isinstance(m, str)]

    meta = root.setdefault("metadata", {})
    current_modules = meta.get("modules")
    if not isinstance(current_modules, list):
        current_modules = []
    current_settings = meta.get("module_settings")
    if not isinstance(current_settings, dict):
        current_settings = {}

    if "modules" in payload:
        new_modules = _modules_mod.filter_active(payload.get("modules"), available_ids)
        # When adding a module, seed its settings with defaults so the
        # UI has something to render. When dropping, retain its
        # settings dict (cheap; lets re-enable be a no-cost toggle).
        for mid in new_modules:
            if mid not in current_settings:
                manifest = _modules_mod.get(mid)
                if manifest:
                    current_settings[mid] = _modules_mod.default_setting_values(manifest)
        meta["modules"] = new_modules
        current_modules = new_modules

    if "module_settings" in payload:
        raw = payload.get("module_settings") or {}
        if isinstance(raw, dict):
            for mid, raw_vals in raw.items():
                if mid not in current_modules:
                    continue
                manifest = _modules_mod.get(mid)
                if not manifest:
                    continue
                # Merge: keep any existing keys the request didn't touch.
                merged = dict(current_settings.get(mid) or {})
                coerced = _modules_mod.coerce_settings(manifest, raw_vals)
                merged.update(coerced)
                current_settings[mid] = merged
        meta["module_settings"] = current_settings

    convs.save_conversation(conv)
    return jsonify({
        "modules": meta.get("modules") or [],
        "module_settings": meta.get("module_settings") or {},
    })


@bp.get("/scenarios/<sid>/setups")
@login_required
def get_scenario_setups(sid: str):
    """Return the resolved setup list for a scenario plus the random
    pools and start toggles. Used by the chat UI for the setup picker
    and by the dashboard pre-creation modal to render the toggles +
    pool pickers before POSTing /conversations.
    """
    from .. import entities as ent
    from ..setups import (
        setup_list,
        random_character_pool,
        random_item_pool,
        start_toggles,
    )
    scen = ent.get(sid)
    if not scen or scen.get("type") != "scenario":
        return jsonify({"error": "not found"}), 404

    def _name(eid: str) -> dict[str, str]:
        e = ent.get(eid) or {}
        return {"id": eid, "name": e.get("name") or eid}

    return jsonify({
        "setups": [
            {"id": s["id"], "name": s["name"], "description": s["description"]}
            for s in setup_list(scen)
        ],
        "random_character_pool": [_name(c) for c in random_character_pool(scen)],
        "random_item_pool": [_name(o) for o in random_item_pool(scen)],
        "start_toggles": start_toggles(scen),
    })


@bp.post("/conversations/<cid>/messages")
@login_required
def append_user_message(cid: str):
    """Append a user-authored message under the current active leaf (or a given parent).

    Modules can contribute arbitrary keys under ``metadata.modules.<id>``
    via the optional ``metadata`` field on the POST body. The texting
    module's compose hook stamps ``metadata.modules.texting = {to:
    <char_id>}``; the prompt-filter the module registers reads it back
    when assembling the responder's prompt. Engine treats the value as
    opaque — schema is per-module.
    """
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    parent_id = payload.get("parent_id") or conv.get("active_path_leaf") or None
    if parent_id and parent_id not in conv["messages"]:
        parent_id = None  # active leaf was deleted; start a fresh root
    content = payload.get("content", "")
    persona = payload.get("persona", "user")
    speaker_id = payload.get("speaker_id")
    extra_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None
    msg = convs.append_message(
        conv,
        parent_id=parent_id,
        persona=persona,
        content=content,
        speaker_id=speaker_id,
        metadata=extra_metadata,
    )
    convs.save_conversation(conv)
    return jsonify(msg), 201


@bp.put("/conversations/<cid>/messages/<mid>")
@login_required
def edit_message(cid: str, mid: str):
    """Edit a message's content. By default forks history into a new
    sibling branch; pass {"mode": "in_place"} to mutate the existing
    message directly (the "Raw" edit action — keeps descendants
    attached, no branch created)."""
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    content = payload.get("content", "")
    try:
        if payload.get("mode") == "in_place":
            new_msg = convs.edit_message_in_place(conv, mid, content)
        else:
            new_msg = convs.edit_message_as_branch(conv, mid, content)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    convs.save_conversation(conv)
    return jsonify(new_msg)


@bp.patch("/conversations/<cid>/messages/<mid>")
@login_required
def patch_message(cid: str, mid: str):
    """In-place metadata patch (used for editing the thinking trace).

    Unlike PUT, this does NOT create a sibling branch; it mutates the
    existing message. Only specific safe fields are accepted.
    """
    conv = convs.load_conversation(cid)
    if not conv or mid not in conv["messages"]:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    msg = conv["messages"][mid]
    if "thinking" in payload:
        meta = msg.setdefault("metadata", {})
        thinking = payload.get("thinking") or ""
        if thinking:
            meta["thinking"] = thinking
        else:
            meta.pop("thinking", None)
    msg["edited_at"] = int(__import__("time").time())
    convs.save_conversation(conv)
    return jsonify(msg)


@bp.delete("/conversations/<cid>/messages/<mid>")
@login_required
def delete_message(cid: str, mid: str):
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    try:
        n = convs.delete_subtree(conv, mid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    convs.save_conversation(conv)
    return jsonify({"deleted": n})


@bp.post("/conversations/<cid>/active-leaf")
@login_required
def set_active_leaf(cid: str):
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    try:
        convs.set_active_leaf(conv, payload.get("leaf_id", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    convs.save_conversation(conv)
    # Return the mirrored setup-driven fields so the client can patch
    # its cached `settings` in place. set_active_leaf -> _mark_active_setup_root
    # rewrites `settings.user_persona` and `settings.scenario_instructions`
    # whenever the new leaf crosses to a different setup root; without
    # echoing them back the UI keeps showing the previous setup's
    # persona / modifiers until a full reload.
    settings = conv.get("settings") or {}
    cast = effective_cast_at(conv)
    setup_root = active_setup_root_for_path(conv)
    return jsonify({
        "active_path_leaf": conv["active_path_leaf"],
        "settings": {
            "user_persona": settings.get("user_persona") or {},
            "scenario_instructions": settings.get("scenario_instructions") or "",
        },
        "effective_user_persona": effective_user_persona(conv),
        "effective_cast": {
            "characters": sorted(cast["characters"]),
            "objects": sorted(cast["objects"]),
        },
        "default_responder": default_responder_for_path(conv) or "",
        "active_setup_root_id": (setup_root or {}).get("id") or "",
    })


@bp.put("/conversations/<cid>/settings")
@login_required
def update_settings(cid: str):
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    conv.setdefault("settings", {}).update(payload)
    convs.save_conversation(conv)
    return jsonify(conv["settings"])


@bp.get("/conversations/<cid>/active-setup")
@login_required
def get_active_setup(cid: str):
    """Return the active setup metadata for the current path, plus the
    aggregated applied-edits timeline. Drives the left-panel "Scenario
    instructions / Scenario edits / Applied edits" sections."""
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    leaf_id = conv.get("active_path_leaf") or ""
    setup = active_setup_for_path(conv, leaf_id) or {}
    root = active_setup_root_for_path(conv, leaf_id)
    timeline = path_applied_edits_with_origin(conv, leaf_id)
    return jsonify({
        "active_path_leaf": leaf_id,
        "setup_root_id": root["id"] if root else None,
        "setup": setup,
        "applied_edits_timeline": timeline,
    })


@bp.put("/conversations/<cid>/active-setup")
@login_required
def update_active_setup(cid: str):
    """Update editable prompt fields on the active setup root.

    Body keys (all optional):
      scenario_instructions_base, scenario_instructions_append,
      system_prompt_character, system_prompt_narrator,
      author_note, author_note_depth, post_history_instructions, state

    Writes the new values onto the root message's `metadata.setup` and
    rebuilds the merged `scenario_instructions`. Does not re-apply
    state edits — that's an explicit re-seed step. Returns the
    updated setup block."""
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    leaf_id = conv.get("active_path_leaf") or ""
    root = active_setup_root_for_path(conv, leaf_id)
    if not root:
        return jsonify({"error": "no active setup root"}), 400
    payload = request.get_json(silent=True) or {}
    setup = root.setdefault("metadata", {}).setdefault("setup", {})

    str_fields = (
        "scenario_instructions_base",
        "scenario_instructions_append",
        "system_prompt_character",
        "system_prompt_narrator",
        "author_note",
        "post_history_instructions",
        "state",
    )
    changed = False
    for k in str_fields:
        if k in payload:
            setup[k] = str(payload[k] or "")
            changed = True
    if "author_note_depth" in payload:
        try:
            setup["author_note_depth"] = int(payload["author_note_depth"])
        except (TypeError, ValueError):
            setup["author_note_depth"] = None
        changed = True
    if "author_note_per_character" in payload:
        val = payload["author_note_per_character"]
        setup["author_note_per_character"] = (
            {str(k): str(v) for k, v in val.items()
             if isinstance(v, str) and v.strip()}
            if isinstance(val, dict) else {}
        )
        changed = True

    if changed:
        base = (setup.get("scenario_instructions_base") or "").strip()
        append = (setup.get("scenario_instructions_append") or "").strip()
        if base and append:
            setup["scenario_instructions"] = base + "\n\n" + append
        else:
            setup["scenario_instructions"] = append or base
        # Mirror into settings so legacy reads stay current. Path-replay
        # is still authoritative for the prompt.
        s = conv.setdefault("settings", {})
        s["scenario_instructions"] = setup["scenario_instructions"]
        for k in (
            "system_prompt_character",
            "system_prompt_narrator",
            "author_note",
            "post_history_instructions",
        ):
            if k in setup:
                s[k] = setup[k] or ""
        if isinstance(setup.get("author_note_depth"), int):
            s["author_note_depth"] = setup["author_note_depth"]
        if isinstance(setup.get("author_note_per_character"), dict):
            s["author_note_per_character"] = setup["author_note_per_character"]
        convs.save_conversation(conv)
    return jsonify({"setup": setup})


# ---------------------------------------------------------------------------
# Image-pack pick
#
# Given a generated character message, ask a model to choose the image from
# the character's image_pack catalog that best fits the response. The model
# returns an entry id; we strictly validate that the id exists in the
# catalog and retry once on failure. Failure means: caller renders no image.
# Always uses the conversation's main model (mirroring narrator_edit /
# auto_state) so the keep-alive-pinned weights stay resident — picking a
# different model here causes Ollama to unload/reload between turns.
# Configurable via:
#   - per-conv: settings.image_pack_pick (true / false / null=use-global)
#   - global default: config.defaults.image_pack_pick (default false)
# ---------------------------------------------------------------------------


_PICK_JSON = re.compile(r"\{[^{}]*\"id\"\s*:\s*(-?\d+)[^{}]*\}", re.DOTALL)


def _image_pack_pick_enabled(conv: dict) -> bool:
    s = (conv.get("settings") or {}).get("image_pack_pick")
    if s is True or s is False:
        return s
    defaults = current_app.config.get("defaults") or {}
    return bool(defaults.get("image_pack_pick", False))


def _auto_state_changes_enabled(conv: dict) -> bool:
    """Per-conversation toggle for the auto-state-changes side call.
    Same three-state precedence as ``_image_pack_pick_enabled``: per-conv
    override wins, then global default, then false."""
    s = (conv.get("settings") or {}).get("auto_state_changes")
    if s is True or s is False:
        return s
    defaults = current_app.config.get("defaults") or {}
    return bool(defaults.get("auto_state_changes", False))


def _auto_state_aspect_enabled(conv: dict, key: str) -> bool:
    """Per-aspect Auto State toggle (auto_state_transparency,
    auto_state_location, …). Per-conv override wins, then global
    default, then false."""
    s = (conv.get("settings") or {}).get(key)
    if s is True or s is False:
        return s
    defaults = current_app.config.get("defaults") or {}
    return bool(defaults.get(key, False))


def _auto_state_on_user_enabled(conv: dict) -> bool:
    """Sub-toggle: also run auto-state on USER messages, not just NPC
    ones. Off by default — extra model round-trip per user turn is
    only useful when the user's own prose narrates state changes
    ("I take off my shirt") that should propagate to entity state.
    Requires the parent ``auto_state_changes`` toggle to also be on;
    a user-only enable is silently no-op without the parent."""
    s = (conv.get("settings") or {}).get("auto_state_on_user_messages")
    if s is True or s is False:
        return s
    defaults = current_app.config.get("defaults") or {}
    return bool(defaults.get("auto_state_on_user_messages", False))


def _auto_state_on_narrator_enabled(conv: dict) -> bool:
    """Sub-toggle: run a full narrator-add pass over user-typed
    narrator messages. Different from the wardrobe-only auto-state
    side call — narrator-add can move characters, materialize
    off-cast NPCs, set status notes, create objects, etc. Useful
    when the user types a narrator beat like *"two guys walk in"*
    and wants the system to auto-encode the implied state changes
    (cast_add + move + outfit) without having to invoke
    narrator-add manually.

    Off by default — narrator prose is often descriptive without
    needing state changes, and the full narrator-add pass is more
    expensive than the wardrobe-only auto-state. Requires the parent
    ``auto_state_changes`` toggle to also be on."""
    s = (conv.get("settings") or {}).get("auto_state_on_narrator_messages")
    if s is True or s is False:
        return s
    defaults = current_app.config.get("defaults") or {}
    return bool(defaults.get("auto_state_on_narrator_messages", False))


_PICK_SYSTEM_TEMPLATE = """\
You are picking the image from a catalog that best matches a character's response in an interactive roleplay scene.

Each catalog entry is an integer id paired with a danbooru-style tag caption — a comma-separated list of visual tags covering pose, gesture, outfit, expression, and setting. The character's response is prose: it describes actions, dialogue, and emotion.

Your job: pick the single entry whose tags best line up with what the response actually depicts.

Rules:
1. Reply with ONLY {{"id": N}} where N is one of the listed ids. No prose, no commentary, no markdown fences, no thinking out loud.
2. Match priority — visible action and pose first (sitting / standing / leaning / holding X / arms crossed / hands on hips), then expression and mood (smile / laugh / serious / surprised / blush), then outfit cues if the response calls them out, then setting if it does.
3. A tag the response neither describes nor contradicts is neutral — neither boost nor penalty. Don't disqualify an entry just because the response is silent on its setting or outfit.
4. When multiple entries are plausible, pick the one with the most matching tags. If still tied, prefer the entry with fewer tags unrelated to the response.
5. Pick the entry that is the most accurate to the response. There is exactly one best answer.

[Worked example]

Character: A
Response:
She sits down on the bench beside you, leans against your shoulder, and watches what you're working on with a quiet smile.

Catalog:
  0: 1girl, standing, arms at sides, neutral expression, indoors
  1: 1girl, sitting on bench, looking at viewer, smiling, indoors
  2: 1girl, sitting on bench, leaning on shoulder, head tilted, smiling, indoors
  3: 1girl, walking forward, surprised expression, outdoors

Expected output:
{{"id": 2}}

(Both 1 and 2 are sitting on the bench, but entry 2 also captures the response's specific action — leaning on the shoulder. When multiple entries share a base pose, pick the one whose extra tags match what the response actually depicts.)

[Character]
{char_name}

[Response]
{response_text}

[Catalog]
{catalog_lines}

Now pick the single best id."""


def _ask_for_pick(catalog: list[dict], char_name: str, response_text: str,
                  model: str | None,
                  options: dict | None) -> tuple[int | None, str, str]:
    """One round-trip to the model. Returns (chosen_id_or_None, prompt, raw).

    `options` is the merged sampling stack the caller wants used — same
    precedence as a normal generate call (per-conversation overrides on
    top of the per-model profile, with config defaults applied inside
    chat_stream). This keeps the chooser behaving like every other call
    in the app instead of running on a separate hardcoded dial.
    """
    catalog_lines = "\n".join(f"  {e['id']}: {e['caption']}" for e in catalog)
    system = _PICK_SYSTEM_TEMPLATE.format(
        char_name=char_name,
        response_text=response_text.strip(),
        catalog_lines=catalog_lines,
    )
    try:
        raw = chat_sync(
            system=system,
            messages=[],
            model=model,
            options=options or {},
            think=False,  # JSON id pick, never reason
        )
    except Exception:
        return None, system, ""
    chosen: int | None = None
    try:
        chosen = int(json.loads(raw.strip()).get("id"))
    except (ValueError, TypeError, json.JSONDecodeError):
        m = _PICK_JSON.search(raw)
        if m:
            try:
                chosen = int(m.group(1))
            except ValueError:
                chosen = None
    return chosen, system, raw


def _sprite_host() -> str:
    """Base URL for the sprite-image host. Configured via
    `config.image_host` (top-level) so the deployment can re-point it
    away from the default `example.com:5000` without touching
    code or per-character data."""
    return (current_app.config.get("image_host") or "").strip()


def _resolve_sprite_state(
    cid: str, conv: dict, msg: dict, speaker_id: str,
    *,
    eff: dict[str, dict] | None = None,
) -> dict | None:
    """Resolve the per-speaker render state for a combined-format
    character at the moment they spoke. Returns
    ``{sprite_id, slots, garments, scene_tag, character, outfit, room,
    location}`` or None when the speaker isn't combined-format.

    Shared by ``_maybe_sprite_pick`` (single-character) and
    ``_maybe_group_sprite_pick`` (multi-character side-by-side) so the
    same outfit / clothing_slots / clothing_overrides / scene-tag
    resolution rules drive both code paths.

    Default `eff` is computed at ``msg["id"]`` (not the parent) so any
    `applied_edits` recorded on the message itself — narrator-edit
    rewrites, embedded `[set ...]` directives extracted by
    persist_partial, scene-staging edits on a sibling root — reflect
    in the same message's image. Callers that explicitly want the
    pre-message state can still pass an `eff` computed elsewhere.
    """
    if eff is None:
        leaf_id = msg.get("id") or msg.get("parent_id") or ""
        eff = effective_entities_at(conv, leaf_id) if leaf_id else {}
    speaker = (
        eff.get(speaker_id)
        or ent.load_instance_entity(cid, speaker_id)
        or ent.get(speaker_id)
    )
    # B-sides with no image config of their own render via the A-side's sprite.
    from .. import bside
    speaker = bside.image_view(speaker, eff)
    if not sprite.has_sprite(speaker):
        return None

    # `sprite.sprite_id_of` reads `properties.images.sprite_id` first
    # and falls back to the legacy top-level `properties.sprite_id`,
    # matching `has_sprite` above.
    sprite_id = sprite.sprite_id_of(speaker)

    # Presence snapshot (set by narrator [outfit] / [move] directives)
    # wins over the character entity's static current_outfit + default
    # location, mirroring how _assemble_character renders these fields.
    snap = (msg.get("presence_snapshot") or {}).get("presence") or {}
    char_pres = snap.get(speaker_id) or {}
    outfit_id = (
        char_pres.get("outfit")
        or (speaker.get("properties") or {}).get("current_outfit")
    )
    location_id = (
        char_pres.get("location")
        or (speaker.get("properties") or {}).get("default_location")
    )
    room_id = (
        char_pres.get("room")
        or (speaker.get("properties") or {}).get("default_room")
    )

    # v2 dual-read: when the character has a `properties.worn` map
    # (the v2 source-of-truth from docs/clothing.md), route slot +
    # garment resolution through `clothing_v2.resolve_sprite_slots_v2`
    # instead of the v1 `outfit.clothing_slots` path. Without this,
    # v2 characters whose outfit bundles don't have `clothing_slots`
    # render as unclothed in sprites — every slot defaults to 3.
    speaker_props = speaker.get("properties") or {}
    use_v2 = "worn" in speaker_props and isinstance(speaker_props.get("worn"), dict)

    if use_v2:
        from .. import clothing_v2
        # Apply v1 clothing_overrides as backcompat translation —
        # narrator still emits v1 directives until step 7 collapses
        # the prompt block. Translates `<slot> = 1/2/3` to v2 state
        # name (semantic, not positional). Returns a copy.
        v2_speaker = clothing_v2.apply_v1_overrides_to_worn(speaker, eff)
        slot_tuple, garment_tuple = clothing_v2.resolve_sprite_slots_v2(
            v2_speaker, eff,
        )
        slots = dict(zip(clothing_v2.SPRITE_SLOT_ORDER, slot_tuple))
        garments_dict = dict(zip(clothing_v2.SPRITE_SLOT_ORDER, garment_tuple))
        # v2 path skips accessory composition — accessories under v2
        # are just clothing pieces in head/face/etc slots, already in
        # the worn map.
        outfit = _resolve_outfit(outfit_id, eff) if outfit_id else None
    else:
        outfit = _resolve_outfit(outfit_id, eff) if outfit_id else None

        # Compose any accessories declared on the character onto the primary
        # outfit so the sprite renderer sees the merged clothing_slots,
        # garments, and `displaces` directives. Text rendering does the same
        # composition in personas._compose_accessories; keeping the resolver
        # in sync here ensures the image and the prose agree about what's
        # on the body. Accessories that don't touch any of the 8 slots (cat
        # ears, tail, tattoo) contribute nothing here — they show only in
        # the text rendering, which is the expected behavior given the
        # current 8-slot sprite system.
        if outfit is not None:
            from ..personas import _compose_accessories
            outfit = _compose_accessories(outfit, speaker, eff)

        slots = dict((outfit or {}).get("properties", {}).get("clothing_slots") or {})
        garments_dict = sprite.garments_of(outfit)

        # Per-character single-slot overrides on top of the outfit preset.
        # Lets the narrator emit `[set <char>.properties.clothing_overrides.<slot> = <state>]`
        # to flip one garment without spinning up a whole new outfit entity
        # (e.g. "no bra under the rolled-up shirt"). Outfit swaps clear the
        # override map (see effective._replay_edit) so presets don't carry
        # stale flips.
        overrides = speaker_props.get("clothing_overrides")
        if isinstance(overrides, dict):
            for slot, value in overrides.items():
                if not isinstance(slot, str):
                    continue
                try:
                    n = int(value)
                except (TypeError, ValueError):
                    continue
                if n in (1, 2, 3):
                    slots[slot.lower()] = n

    # Per-character transparency override (0..100 per slot). Lets the
    # narrator emit `[set <char>.properties.clothing_transparency.<slot>
    # = <pct>]` to fade a layer toward see-through without picking a
    # whole new outfit. Cleared on outfit swap (effective._replay_edit)
    # the same way clothing_overrides is.
    transparency: dict[str, int] = {}
    raw_tr = (speaker.get("properties") or {}).get("clothing_transparency")
    if isinstance(raw_tr, dict):
        for slot, value in raw_tr.items():
            if not isinstance(slot, str):
                continue
            try:
                n = int(value)
            except (TypeError, ValueError):
                continue
            if n < 0:
                n = 0
            elif n > 100:
                n = 100
            if n < 100:
                transparency[slot.lower()] = n

    room = eff.get(room_id) if room_id else None
    location = eff.get(location_id) if location_id else None
    scene_tag = sprite.resolve_scene_tag(
        room=room, location=location, character=speaker
    )

    return {
        "sprite_id": sprite_id,
        "slots": slots,
        "garments": garments_dict,
        "transparency": transparency,
        "scene_tag": scene_tag,
        "character": speaker,
        "outfit": outfit,
        "outfit_id": outfit_id,
        "room": room,
        "room_id": room_id,
        "location": location,
        "location_id": location_id,
    }


def _maybe_sprite_pick(
    cid: str, conv: dict, msg: dict, speaker_id: str,
    *,
    eff: dict[str, dict] | None = None,
) -> dict | None:
    """Build a sprite URL for the speaker if they carry `sprite_id`.

    Reads effective state at the message's parent so the image reflects
    where/what the character was when she spoke, not stale baseline.
    Returns the same `image_pack_pick` shape the catalog flow returns,
    so the frontend renders both identically.

    `eff` lets callers pass a pre-computed effective-entities dict — the
    streaming pipeline already computed one for prompt assembly, so
    skipping a second O(path) replay here saves real work on long
    conversations.
    """
    state = _resolve_sprite_state(cid, conv, msg, speaker_id, eff=eff)
    if state is None:
        return None

    # Empty host = serve from the local /sprites blueprint; non-empty
    # host = remote sprite server. Either way, build_url returns a
    # browser-fetchable URL.
    host = _sprite_host()
    url = sprite.build_url(
        host=host,
        sprite_id=str(state["sprite_id"]),
        clothing_slots=state["slots"],
        scene_tag=state["scene_tag"],
        garments=state["garments"],
        transparency=state.get("transparency"),
    )
    caption = sprite.caption_for(
        character=state["character"],
        outfit=state["outfit"],
        room=state["room"],
        location=state["location"],
    )
    return {
        "id": "sprite",
        "image_url": url,
        "caption": caption,
        "trace": {
            "source": "sprite",
            "sprite_id": state["sprite_id"],
            "outfit_id": state["outfit_id"],
            "room_id": state["room_id"],
            "location_id": state["location_id"],
            "scene_tag": state["scene_tag"] or sprite.DEFAULT_SCENE,
            "clothing_slots": state["slots"] or {},
        },
    }


def _maybe_group_sprite_pick(
    cid: str, conv: dict, group_msgs: list[dict],
    *,
    eff: dict[str, dict] | None = None,
) -> dict | None:
    """Build a side-by-side composite URL for the multi-response group.

    Resolves per-speaker render state for every member via
    ``_resolve_sprite_state``; filters to combined-format speakers
    only; emits a single ``image_pack_pick`` record pointing at
    ``/sprites/group/<scene>/<spec>/<spec>/...``. The shared scene_tag
    is the lead's resolved scene — every partner uses it.

    Returns None when fewer than 2 group members are combined-format
    (so the caller can fall back to per-character picks).
    """
    if not group_msgs:
        return None
    participants: list[dict] = []
    captions: list[str] = []
    trace_members: list[dict] = []
    scene_tag: str | None = None
    for m in group_msgs:
        speaker_id = m.get("speaker_id") or ""
        if not speaker_id:
            continue
        state = _resolve_sprite_state(cid, conv, m, speaker_id, eff=eff)
        if state is None:
            continue
        participants.append({
            "sprite_id": state["sprite_id"],
            "clothing_slots": state["slots"],
            "garments": state["garments"],
            "transparency": state.get("transparency"),
        })
        captions.append(sprite.caption_for(
            character=state["character"],
            outfit=state["outfit"],
            room=state["room"],
            location=state["location"],
        ))
        trace_members.append({
            "speaker_id": speaker_id,
            "sprite_id": state["sprite_id"],
            "outfit_id": state["outfit_id"],
            "room_id": state["room_id"],
            "location_id": state["location_id"],
            "clothing_slots": state["slots"] or {},
        })
        if scene_tag is None:
            # Lead's scene wins — partners share it.
            scene_tag = state["scene_tag"]

    if len(participants) < 2:
        return None

    host = _sprite_host()
    url = sprite.build_group_url(
        host=host,
        participants=participants,
        scene_tag=scene_tag or sprite.DEFAULT_SCENE,
    )
    caption = " · ".join(captions)
    return {
        "id": "group_sprite",
        "image_url": url,
        "caption": caption,
        "trace": {
            "source": "group_sprite",
            "scene_tag": scene_tag or sprite.DEFAULT_SCENE,
            "members": trace_members,
        },
    }


def _resolve_outfit(
    outfit_id: str, entities: dict[str, dict]
) -> dict:
    """Walk properties.extends child→base, shallow-merging properties so
    a per-character outfit can inherit clothing_slots from a base. Same
    behaviour as personas._resolved_outfit, kept here to avoid pulling
    that internal into the API surface."""
    seen: set[str] = set()
    chain: list[dict] = []
    cur: str | None = outfit_id
    while cur and cur not in seen and cur in entities:
        seen.add(cur)
        e = entities[cur]
        chain.append(e)
        nxt = (e.get("properties") or {}).get("extends")
        cur = nxt if isinstance(nxt, str) else None
    if not chain:
        return {}
    if len(chain) == 1:
        return chain[0]
    merged: dict = {}
    for e in reversed(chain):
        for k, v in e.items():
            if k == "properties" and isinstance(v, dict) and isinstance(merged.get("properties"), dict):
                merged["properties"] = {**merged["properties"], **v}
            else:
                merged[k] = v
    return merged


@bp.post("/conversations/<cid>/messages/<mid>/image_pick")
@login_required
def image_pack_pick(cid: str, mid: str):
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "conversation not found"}), 404
    msg = (conv.get("messages") or {}).get(mid)
    if not msg:
        return jsonify({"error": "message not found"}), 404

    # Effective enabled — per-conv overrides global default.
    if not _image_pack_pick_enabled(conv):
        return jsonify({"picked": None, "reason": "disabled"})

    speaker_id = msg.get("speaker_id")
    if not speaker_id:
        return jsonify({"picked": None, "reason": "no speaker"})

    # Combined-format characters (Iris / Dex / Rosa): build the
    # image URL deterministically from current outfit + room state. Falls
    # through to the tagged-catalog flow when the character isn't combined.
    pick_record = _maybe_sprite_pick(cid, conv, msg, speaker_id)
    if pick_record:
        msg.setdefault("metadata", {})["image_pack_pick"] = pick_record
        convs.save_conversation(conv)
        return jsonify({"picked": pick_record})

    # Tagged-catalog path (tagged-image style): each entry pairs a booru-style
    # caption with a hosted URL; a second model call picks the best match
    # for the character's prose. Reads `properties.images.entries` first
    # and falls back to the legacy `properties.image_pack.entries` so
    # a legacy tagged character's existing data keeps working untouched.
    #
    # The speaker is the path-effective view, not the raw instance file:
    # branch-scoped overlays (the cast panel's per-character image-pack
    # toggles, `[set … enabled_image_packs]` directives) land as
    # `kind=patch` edits on the leaf and must shape the catalog.
    speaker = None
    try:
        speaker = effective_entities_at(
            conv, conv.get("active_path_leaf") or None
        ).get(speaker_id)
    except Exception:
        speaker = None
    if speaker is None:
        speaker = ent.load_instance_entity(cid, speaker_id) or ent.get(speaker_id)
    # B-sides with no image pack of their own inherit the A-side's catalog.
    from .. import bside
    speaker = bside.image_view(speaker)
    # Scene tags expose image packs the same way they expose conditional
    # pairs: a pack declaring `expose_tags` auto-enables when one of those
    # tags is present in the scene (objects present + the focal's room +
    # the outfit they're wearing). Gathered defensively — any miss degrades
    # to "no tag-exposed packs", never an error.
    scene_tags: set[str] = set()
    try:
        msgs = conv.get("messages") or {}
        leaf = msgs.get(conv.get("active_path_leaf") or "") or {}
        snap = leaf.get("presence_snapshot") or {}
        # Objects in scene = the branch's cast objects (path-replayed
        # cast_add/cast_remove — staging picks, narrator edits, the side
        # panel's add) plus any snapshot objects_present (scenarios that
        # seed starting_state.objects_present).
        present_objs = set(snap.get("objects_present") or {})
        present_objs |= effective_cast_at(conv).get("objects") or set()
        for obj_id in present_objs:
            obj = ent.load_instance_entity(cid, obj_id) or ent.get(obj_id)
            if obj:
                scene_tags.update(str(t).lower() for t in (obj.get("tags") or []))
        pres = (snap.get("presence") or {}).get(speaker_id) or {}
        room_id = pres.get("room")
        room = (ent.load_instance_entity(cid, room_id) or ent.get(room_id)) if room_id else None
        if room:
            scene_tags.update(str(t).lower() for t in (room.get("tags") or []))
        cur = ((speaker or {}).get("properties") or {}).get("current_outfit") or pres.get("outfit")
        bundle = (ent.load_instance_entity(cid, cur) or ent.get(cur)) if cur else None
        if bundle:
            scene_tags.update(str(t).lower() for t in (bundle.get("tags") or []))
    except Exception:
        scene_tags = set()
    catalog_full = [
        {"id": i, "caption": e.get("caption", ""), "image_url": e.get("image_url")}
        for i, e in enumerate(sprite.tagged_entries_of(speaker, scene_tags))
        if e and e.get("image_url")
    ]
    if not catalog_full:
        return jsonify({"picked": None, "reason": "empty catalog"})

    # Image pools: narrow the catalog by the character's per-variant pool
    # states (on / off / excluded) before scoring, so the per-turn pick
    # respects which tagged sub-sets the user has enabled/disabled/excluded.
    # Empty result falls back to the full catalog (see filter_entries_by_pools).
    # Back-compat: an older single-select `current_outfit_profile` pin still
    # applies on top when no pool states are set.
    profiles = sprite.outfit_profiles_of(speaker)
    pool_states = sprite.image_pool_states_of(speaker)
    if profiles and pool_states:
        catalog_full = sprite.filter_entries_by_pools(catalog_full, profiles, pool_states)
    else:
        profile_id = sprite.current_outfit_profile_of(speaker)
        profile = profiles.get(profile_id) if profile_id else None
        if profile:
            catalog_full = sprite.filter_entries_by_profile(catalog_full, profile)

    catalog_for_model = [{"id": e["id"], "caption": e["caption"]} for e in catalog_full]
    valid_ids = {e["id"] for e in catalog_full}
    char_name = (speaker or {}).get("name") or speaker_id
    text = msg.get("content") or ""

    # Same model + sampling resolution as narrator_edit / auto_state —
    # the conversation's override if any, else the global config model;
    # per-conv sampling layered on top of the per-model profile. Routing
    # this side call through a different model would force Ollama to
    # unload the main model between every turn.
    settings = conv.get("settings") or {}
    model = settings.get("ollama_model_override") or (
        current_app.config.get("ollama") or {}
    ).get("model")
    profile = (current_app.config.get("model_profiles") or {}).get(model) or {}
    sampling = {**profile, **(settings.get("sampling") or {})}

    # Try once, retry once if the result isn't in the catalog.
    chosen_id, prompt_used, raw_reply = _ask_for_pick(
        catalog_for_model, char_name, text, model, sampling
    )
    if chosen_id not in valid_ids:
        chosen_id, prompt_used, raw_reply = _ask_for_pick(
            catalog_for_model, char_name, text, model, sampling
        )
    if chosen_id not in valid_ids:
        return jsonify({
            "picked": None,
            "reason": "validation failed after retry",
            "trace": {"prompt": prompt_used, "reply": raw_reply},
        })

    picked = next(e for e in catalog_full if e["id"] == chosen_id)
    pick_record = {
        "id": picked["id"],
        "image_url": picked["image_url"],
        "caption": picked["caption"],
        "trace": {"prompt": prompt_used, "reply": raw_reply},
    }
    # Persist on the message so reloads (and exports) carry the image
    # and its reasoning trace, the same way thinking is kept in
    # msg.metadata.thinking. Subsequent calls for the same message can
    # short-circuit without hitting the model again.
    msg.setdefault("metadata", {})["image_pack_pick"] = pick_record
    convs.save_conversation(conv)
    return jsonify({"picked": pick_record})


def _run_full_narrator_pass_on_narrator_msg(conv: dict, msg: dict) -> Any:
    """Auto-state on a narrator message routes through the narrator-add
    side call (full prompt: cast / off-cast / outfits / rooms /
    wardrobe-extra / user controls) instead of the wardrobe-only
    auto-state prompt. The narrator message body becomes the directive
    — same shape the explicit "Narrator additions" side panel uses.

    Appends the result as a SIBLING of the target narrator message
    (parent = target's parent) — same branching shape narrator-edit
    uses. This preserves the original user-typed beat as a peer
    branch the user can flip back to; the auto-state rewrite lives
    as the new leaf. Mirrors narrator_edit.append_narrator_edit_result
    behavior so path-replay (and the cast widget's branch-cast view)
    consume both branches identically.

    Returns the same JSON shape `auto_state_pass` does so the client
    can render the resulting block + refresh state seamlessly. The
    response carries the NEW sibling's id (`new_message_id`) so the
    client can fold it into local state and set it as the active
    leaf.
    """
    cid = conv["id"]
    mid = msg["id"]
    directive = (msg.get("content") or "").strip()
    if not directive:
        return jsonify({
            "ran": False, "skipped": "narrator_message_empty",
        })

    from ..narrator_add import narrator_add_message_sync
    from ..narrator_edit import append_narrator_edit_result
    settings = conv.get("settings", {}) or {}
    model = settings.get("ollama_model_override") or (
        current_app.config.get("ollama") or {}
    ).get("model")
    profile = (current_app.config.get("model_profiles") or {}).get(model) or {}
    sampling = {**profile, **(settings.get("sampling") or {})}

    try:
        result = narrator_add_message_sync(
            conv, mid, directive,
            model=model, options=sampling, think=False,
        )
    except Exception as e:
        current_app.logger.exception("auto_state narrator full call failed")
        return jsonify({
            "ran": False, "skipped": "narrator_call_error",
            "error": str(e),
        })

    edits = list(result.get("edits") or [])
    new_body = (result.get("new_body") or "").strip() or directive
    raw = result.get("raw_response") or ""

    if not edits and not new_body:
        # Narrator had nothing to add. Stamp the empty result on the
        # ORIGINAL msg so the client doesn't re-fire on next mount.
        msg.setdefault("metadata", {})["auto_state_changes"] = {
            "edits": [], "raw_response": raw, "mode": "narrator_full",
        }
        convs.save_conversation(conv)
        return jsonify({
            "ran": True, "skipped": None, "edits": [],
            "applied_log": [], "image_pack_pick": None,
            "mode": "narrator_full",
            "group_image_updated_message_ids": [],
            "new_message_id": None,
        })

    # Branch the rewrite as a SIBLING of the user-typed narrator beat
    # (same parent). append_narrator_edit_result applies edits +
    # builds the new presence_snapshot + persists the new message +
    # writes metadata.narrator_edit AND metadata.applied_edits onto
    # the new sibling. Then we ALSO stamp `auto_state_changes` so
    # the client's auto-state-block attachment renders the result on
    # the new sibling.
    try:
        new_msg = append_narrator_edit_result(
            conv, mid,
            directive=directive,
            raw_response=raw,
            new_body=new_body,
            edits=edits,
            thinking_text="",
            reason="auto_state_narrator_full",
        )
    except Exception as e:
        current_app.logger.exception("auto_state narrator append failed")
        return jsonify({
            "ran": False, "skipped": "narrator_append_error",
            "error": str(e),
        })

    new_msg.setdefault("metadata", {})["auto_state_changes"] = {
        "edits": edits,
        "applied_log": ((new_msg.get("metadata") or {}).get("applied_edits") or []),
        "raw_response": raw,
        "mode": "narrator_full",
    }
    # Also stamp a tiny breadcrumb on the original so the client knows
    # the auto-state ran (and skips on re-mount) — but the actual
    # edits live on the new sibling.
    msg.setdefault("metadata", {})["auto_state_changes"] = {
        "edits": [], "raw_response": "",
        "mode": "narrator_full",
        "branched_to": new_msg["id"],
    }
    convs.save_conversation(conv)

    return jsonify({
        "ran": True, "skipped": None,
        "edits": edits,
        "applied_log": (new_msg.get("metadata") or {}).get("applied_edits") or [],
        "new_body": new_msg.get("content"),
        "new_message_id": new_msg["id"],
        "active_path_leaf": conv.get("active_path_leaf"),
        "image_pack_pick": None,
        "mode": "narrator_full",
        "group_image_updated_message_ids": [],
    })


@bp.post("/conversations/<cid>/messages/<mid>/auto_state")
@login_required
def auto_state_pass(cid: str, mid: str):
    """Run the auto-state-changes side call for a single message.

    Fired by the browser after the SSE stream for that message closes
    (chat.js / maybeAutoState). Doing this in a separate POST instead
    of inline in the SSE finalisation keeps the streaming response
    from blocking on the second round-trip — the same shape the
    catalog ``image_pack_pick`` uses.

    Uses the SAME model + sampling stack as the conversation's main
    generate (mirroring narrator_edit). The keep-alive pin keeps the
    main model resident in VRAM, so this is a fast warm round-trip
    instead of a cold load.

    Re-runs the inline sprite pick after applying any emitted edits so
    the rendered image picks up the corrected state in one round trip.
    """
    from ..auto_state import run_and_apply

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "conversation not found"}), 404
    msg = (conv.get("messages") or {}).get(mid)
    if not msg:
        return jsonify({"error": "message not found"}), 404
    clothing_on = _auto_state_changes_enabled(conv)
    transparency_on = _auto_state_aspect_enabled(conv, "auto_state_transparency")
    location_on = _auto_state_aspect_enabled(conv, "auto_state_location")
    if not (clothing_on or transparency_on or location_on):
        return jsonify({"ran": False, "skipped": "disabled"})
    persona = msg.get("persona")
    if persona == "narrator":
        # Narrator beats don't represent any single character's wardrobe.
        # Without the sub-toggle, skip outright — wardrobe-only auto-state
        # has nothing useful to say about narrator prose. WITH the sub-
        # toggle, fire a FULL narrator-add pass instead: the same prompt
        # the user-invoked narrator additions panel uses, with the
        # narrator message body as the directive. Lets a user-typed beat
        # like *"three coworkers pile into the elevator"* auto-encode the
        # implied cast_add + move + outfit edits.
        if not _auto_state_on_narrator_enabled(conv):
            return jsonify({"ran": False, "skipped": "narrator_message"})
        return _run_full_narrator_pass_on_narrator_msg(conv, msg)
    speaker_id = msg.get("speaker_id")
    if persona == "user":
        # User messages are gated behind the sub-toggle because (a)
        # they double the auto-state model-call frequency and (b)
        # most users don't narrate their own clothing changes in
        # every turn. When enabled, treat "user" as the focal speaker
        # — the user entity has the same body_parts / current_outfit
        # / clothing_overrides shape any character does, so the
        # existing auto_state prompt + apply pipeline both compose
        # cleanly.
        if not _auto_state_on_user_enabled(conv):
            return jsonify({"ran": False, "skipped": "user_message_disabled"})
        speaker_id = "user"
    elif not speaker_id:
        return jsonify({"ran": False, "skipped": "not_character_message"})

    # Same model + sampling resolution as narrator_edit — the
    # conversation's override if any, else the global config model;
    # per-conv sampling layered on top of the per-model profile.
    settings = conv.get("settings", {}) or {}
    model = settings.get("ollama_model_override") or (
        current_app.config.get("ollama") or {}
    ).get("model")
    profile = (current_app.config.get("model_profiles") or {}).get(model) or {}
    sampling = {**profile, **(settings.get("sampling") or {})}

    # The main-gen edits live in metadata.applied_edits — same gate
    # the inline path used.
    main_gen_edits = (msg.get("metadata") or {}).get("applied_edits") or []
    if clothing_on:
        summary = run_and_apply(
            conversation=conv,
            msg=msg,
            speaker_id=speaker_id,
            main_gen_edits=main_gen_edits,
            model=model,
            options=sampling,
        )
    else:
        summary = {"ran": False, "skipped": "clothing_disabled",
                   "edits": [], "applied_log": [], "raw": ""}

    # Extra Auto State passes (transparency, location) — each gated by
    # its own panel toggle, applied onto the same message. New passes
    # slot in via auto_state._EXTRA_PASSES.
    from ..auto_state import run_extra_pass
    merged_edits = list(summary.get("edits") or [])
    merged_applied = list(summary.get("applied_log") or [])
    for pass_id, on in (("transparency", transparency_on), ("location", location_on)):
        if not on:
            continue
        ps = run_extra_pass(
            conversation=conv, msg=msg, speaker_id=speaker_id,
            pass_id=pass_id, model=model, options=sampling,
        )
        if ps.get("applied_log"):
            merged_edits.extend(ps.get("edits") or [])
            merged_applied.extend(ps["applied_log"])
    if merged_applied:
        summary["ran"] = True
        summary["skipped"] = None
    summary["edits"] = merged_edits
    summary["applied_log"] = merged_applied

    # Re-run the sprite pick if we changed anything, so the client gets
    # the corrected image in this same response.
    #
    # GATED on `_image_pack_pick_enabled(conv)` — image mode is a
    # per-conv toggle (settings.image_pack_pick) with a global default
    # (config.defaults.image_pack_pick, default false). The auto-state
    # flow used to fire the sprite pick unconditionally whenever it
    # applied any clothing edit, which had the side effect of stamping
    # `metadata.image_pack_pick` on messages in conversations where
    # the user had image mode OFF — effectively flipping image mode
    # on without the user asking. The gate restores the invariant:
    # auto-state mutates clothing state, but image rendering is the
    # user's own toggle decision.
    #
    # Multi-response group members need GROUP re-stamping, not solo —
    # otherwise auto-state on Iris (lead) overrides her image with
    # a solo Iris-in-new-outfit, while Dex (partner) keeps the
    # stale group composite built before Iris's auto-state edits.
    # End result: Iris solo casual, Dex shows the pre-edit group.
    # Re-running the group pick rebuilds the composite from CURRENT
    # state and stamps every member so the whole row stays consistent.
    new_pick = None
    group_updated_ids: list[str] = []
    if summary["applied_log"] and _image_pack_pick_enabled(conv):
        group_id = ((msg.get("metadata") or {})
                    .get("multi_response") or {}).get("group_id")
        try:
            if group_id:
                # Collect every group member on the active path so
                # _maybe_group_sprite_pick can resolve each speaker's
                # current sprite state and emit a side-by-side composite.
                leaf = conv.get("active_path_leaf") or msg["id"]
                chain = convs.path_to_root(conv, leaf) if leaf else []
                members = [
                    m for m in chain
                    if ((m.get("metadata") or {}).get("multi_response") or {}).get("group_id") == group_id
                ]
                members.sort(key=lambda m: ((m.get("metadata") or {}).get("multi_response") or {}).get("ordinal", 0))
                if len(members) >= 2:
                    new_pick = _maybe_group_sprite_pick(cid, conv, members)
                    if new_pick:
                        for member in members:
                            member.setdefault("metadata", {})["image_pack_pick"] = new_pick
                            group_updated_ids.append(member["id"])
                # Fewer than 2 combined-format members on the path —
                # fall back to the solo pick as before.
                if not new_pick:
                    new_pick = _maybe_sprite_pick(cid, conv, msg, speaker_id)
                    if new_pick:
                        msg.setdefault("metadata", {})["image_pack_pick"] = new_pick
            else:
                new_pick = _maybe_sprite_pick(cid, conv, msg, speaker_id)
                if new_pick:
                    msg.setdefault("metadata", {})["image_pack_pick"] = new_pick
        except Exception:
            current_app.logger.exception(
                "auto_state sprite re-pick failed cid=%s mid=%s", cid, mid
            )

    if summary["ran"] and summary["applied_log"]:
        convs.save_conversation(conv)

    return jsonify({
        "ran": summary["ran"],
        "skipped": summary["skipped"],
        "edits": summary["edits"],
        "applied_log": summary["applied_log"],
        "image_pack_pick": new_pick,
        # When the new pick is a group composite, list every member
        # whose image_pack_pick was updated so the client can refresh
        # all their bubbles in one go instead of only the one whose
        # auto_state POST returned.
        "group_image_updated_message_ids": group_updated_ids,
    })


# NOTE: the life_sim_update and life_sim/goal routes moved into the
# self-contained life_sim module at data/modules/life_sim/life_sim.py
# as part of the Phase 2 module refactor. The module declares its own
# Flask blueprint and registers it with the /api prefix at import
# time, so existing chat.js URLs continue to resolve unchanged.


@bp.post("/conversations/<cid>/user/persona-fields")
@login_required
def set_user_persona_fields(cid: str):
    """Set the user's free-text persona fields (name + description) on
    the active branch via the same patch-edit pipeline staging uses.

    Body: {name?: str, description?: str}

    Each provided field becomes a `[set user.<field> = ...]` patch.
    Result: the values land on the user instance entity, ride
    path-replay, and surface to all downstream renders (prompt build,
    /active-leaf, future side-panel reloads) via
    `effective_user_persona`.

    This is the symmetric counterpart to `/user/role` — that one sets
    role/role_description, this one sets name/description. Both emit
    patches against the same `user` entity; both ride the same
    path-replay log.
    """
    from ..narrator_apply import apply_edits

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "conversation not found"}), 404
    payload = request.get_json(silent=True) or {}
    leaf_id = conv.get("active_path_leaf") or ""

    edits: list[dict] = []
    name = payload.get("name")
    description = payload.get("description")
    tags = payload.get("tags")
    if isinstance(name, str) and name.strip():
        edits.append({"kind": "patch", "id": "user", "data": {"name": name.strip()}})
    if isinstance(description, str):
        edits.append({"kind": "patch", "id": "user", "data": {"description": description}})
    # Persona tags drive which context dialogue-pair sets surface for the
    # cast (the `user_tag` selector). Stored on the user entity's
    # `persona_tags` so they don't clobber the reserved "user" tag.
    clean_tags = None
    if isinstance(tags, list):
        clean_tags = [t.strip().lower() for t in tags if isinstance(t, str) and t.strip()]
        edits.append({"kind": "patch", "id": "user", "data": {"persona_tags": clean_tags}})
    if not edits:
        return jsonify({"error": "name, description, or tags required"}), 400

    leaf_msg = (conv.get("messages") or {}).get(leaf_id) or {}
    parent_snap = leaf_msg.get("presence_snapshot") or {}
    user_persona = (conv.get("settings") or {}).get("user_persona") or {}
    existing_cast = effective_cast_at(conv, leaf_id).get("characters") or set()
    _patch, applied_log = apply_edits(
        cid, edits, parent_snap, user_persona=user_persona,
        existing_cast_chars=existing_cast,
    )

    # Mirror name/description onto settings.user_persona so anything
    # still reading the legacy shape (macro engine fallback, etc.)
    # stays in sync with the just-applied patch.
    settings_persona = dict(conv.setdefault("settings", {}).get("user_persona") or {})
    if isinstance(name, str) and name.strip():
        settings_persona["name"] = name.strip()
    if isinstance(description, str):
        settings_persona["description"] = description
    if clean_tags is not None:
        settings_persona["tags"] = clean_tags
    conv["settings"]["user_persona"] = settings_persona

    msg = convs.append_message(
        conv,
        parent_id=leaf_id or None,
        persona="narrator",
        content="[Persona updated]",
        speaker_id=None,
        metadata={"applied_edits": applied_log},
    )
    convs.save_conversation(conv)
    return jsonify({"message": msg, "applied_log": applied_log})


@bp.post("/conversations/<cid>/user/role")
@login_required
def set_user_role(cid: str):
    """Set or clear the user's role overlay (role + role_description)
    on the active branch. Emits patch / unset edits via the standard
    narrator_apply pipeline so the change rides path-replay alongside
    every other branch-scoped state mutation.

    Body shape:
      {role: "Federation liaison", role_description: "..."}
      {clear: true}              — drops both role and role_description
      {clear: true, silent: true} — same, but appends NO narrator message
                                    (used when a pf1e build finalize clears a
                                    stale scenario role as a side-effect, so the
                                    transcript isn't spammed with "[Role cleared]")
    """
    from ..narrator_apply import apply_edits

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "conversation not found"}), 404
    payload = request.get_json(silent=True) or {}
    leaf_id = conv.get("active_path_leaf") or ""

    edits: list[dict] = []
    if payload.get("clear"):
        edits.append({"kind": "unset", "id": "user", "path": ["role"]})
        edits.append({
            "kind": "unset", "id": "user", "path": ["role_description"],
        })
        note = "[Role cleared]"
    else:
        role = payload.get("role")
        role_desc = payload.get("role_description")
        if not isinstance(role, str) or not role.strip():
            return jsonify({"error": "role required"}), 400
        edits.append({"kind": "patch", "id": "user", "data": {"role": role.strip()}})
        edits.append({
            "kind": "patch", "id": "user",
            "data": {
                "role_description": role_desc.strip()
                    if isinstance(role_desc, str) else "",
            },
        })
        note = f"[Role set: {role.strip()}]"

    leaf_msg = (conv.get("messages") or {}).get(leaf_id) or {}
    parent_snap = leaf_msg.get("presence_snapshot") or {}
    user_persona = (conv.get("settings") or {}).get("user_persona") or {}
    existing_cast = effective_cast_at(conv, leaf_id).get("characters") or set()
    _patch, applied_log = apply_edits(
        cid, edits, parent_snap, user_persona=user_persona,
        existing_cast_chars=existing_cast,
    )

    role_change = {
        "role": payload.get("role") or "",
        "role_description": payload.get("role_description") or "",
        "cleared": bool(payload.get("clear")),
    }

    # SILENT clear: when clearing a stale role as a side-effect of a pf1e build
    # finalize, ride path-replay by EXTENDING the active leaf's edit log (the
    # same mechanism studio edits use) instead of appending a "[Role cleared]"
    # narrator turn — so the transcript isn't spammed on every build save. The
    # edits still land on the active path exactly as they would on a new message.
    if payload.get("silent") and leaf_id and leaf_id in (conv.get("messages") or {}):
        leaf = conv["messages"][leaf_id]
        meta = leaf.setdefault("metadata", {})
        log = meta.setdefault("applied_edits", [])
        log.extend(applied_log)
        convs.save_conversation(conv)
        return jsonify({
            "message": None, "applied_log": applied_log,
            "role_change": role_change, "silent": True,
        })

    msg = convs.append_message(
        conv,
        parent_id=leaf_id or None,
        persona="narrator",
        content=note,
        speaker_id=None,
        metadata={
            "role_change": role_change,
            "applied_edits": applied_log,
        },
    )
    convs.save_conversation(conv)
    return jsonify({"message": msg, "applied_log": applied_log})


# ---------------------------------------------------------------------------
# Instance entity edits
# ---------------------------------------------------------------------------


@bp.put("/conversations/<cid>/entities/<eid>")
@login_required
def update_instance_entity(cid: str, eid: str):
    """Per-conv studio editor save (whole-entity replace). Lands as a
    branch-scoped path-replay overlay on the active leaf, not a disk
    write — so editing "Kenji" on branch A doesn't change "Tyler" the
    sibling branch sees.

    The editor opens against the path-effective view (see
    `list_instance_entities`), so the payload represents the user's
    intended FULL state for this entity on this branch. We diff that
    against the effective entity at the leaf and append the diff as a
    `kind=patch` entry onto the leaf's `metadata.applied_edits`. Disk
    baseline is untouched.

    Empty diff = no change; the call is a no-op. UNSET_MARKER entries
    in the diff drop keys via the deep-merge consumer (`merge.deep_merge`).

    Pre-message conversations (no leaf yet) fall back to the legacy
    disk write so the studio editor still works during scenario setup.
    """
    payload = request.get_json(silent=True) or {}
    payload["id"] = eid
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    leaf_id = conv.get("active_path_leaf") or ""
    if not leaf_id:
        try:
            saved = ent.replace_entity(cid, payload)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(saved)
    return _append_studio_edit_to_leaf(conv, eid, payload, replace=True)


@bp.post("/conversations/<cid>/entities/<eid>/patch")
@login_required
def patch_instance_entity(cid: str, eid: str):
    """Shallow-merge studio patch — same branch-scoping rules as the
    PUT handler. Caller sends a sparse dict; we append it directly as
    a `kind=patch` on the active leaf so the prior overlays for
    unchanged fields are preserved.
    """
    payload = request.get_json(silent=True) or {}
    # OPT-IN wholesale replace: a caller (e.g. the pf1e builder saving a finished
    # sheet) can list dot-paths whose subtrees must be REPLACED, not deep-merged,
    # so stale keys under them (scenario-injected junk) can't survive. We drop the
    # named subtrees first, then apply the patch. Consumers that don't send this
    # key are wholly unaffected — the default stays a plain deep-merge patch.
    replace_subtrees = payload.pop("replace_subtrees", None)
    if not isinstance(replace_subtrees, list):
        replace_subtrees = None
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    leaf_id = conv.get("active_path_leaf") or ""
    if not leaf_id:
        try:
            if replace_subtrees:
                ent.unset_paths(cid, eid, replace_subtrees)
            saved = ent.apply_patch(cid, eid, payload)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(saved)
    return _append_studio_edit_to_leaf(
        conv, eid, payload, replace=False, replace_subtrees=replace_subtrees)


def _append_studio_edit_to_leaf(
    conv: dict, eid: str, payload: dict, *, replace: bool,
    replace_subtrees: list | None = None,
):
    """Append a studio edit as a `kind=patch` overlay on the active leaf.

    `replace=True`: payload is a full entity dict (from the PUT handler).
    We diff against the path-effective entity to get the sparse patch,
    so prior overlays for unchanged fields stay in effect.

    `replace=False`: payload IS the sparse patch (from the …/patch
    handler); used verbatim.

    Returns the post-edit effective entity as JSON.
    """
    from ..merge import compute_diff
    cid = conv["id"]
    leaf_id = conv["active_path_leaf"]
    eff_before = effective_entities_at(conv, leaf_id)
    current = eff_before.get(eid)
    if replace:
        if current is None:
            # Brand-new entity on this branch (e.g. studio "create"
            # affordance). Persist the disk baseline once so path-replay
            # has something to replay onto; the patch overlay handles
            # any subsequent edits.
            try:
                ent.replace_entity(cid, payload)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            eff_after = effective_entities_at(conv, leaf_id)
            return jsonify(eff_after.get(eid) or payload)
        diff = compute_diff(current, payload)
    else:
        diff = payload
    # Wholesale-replace: emit an `unset` overlay for each named subtree BEFORE the
    # patch, so path-replay drops the stale subtree (scenario junk) and the patch's
    # deep-merge then sets it fresh. Scoped strictly to the caller-named paths.
    unset_edits = []
    for path in (replace_subtrees or []):
        if isinstance(path, list) and path and all(isinstance(p, str) for p in path):
            unset_edits.append({
                "kind": "unset",
                "id": eid,
                "path": list(path),
                "ok": True,
                "origin": "studio",
            })
    if diff or unset_edits:
        _now = int(time.time())
        for _u in unset_edits:
            _u["made_at"] = _now
        leaf = conv["messages"][leaf_id]
        meta = leaf.setdefault("metadata", {})
        log = meta.setdefault("applied_edits", [])
        log.extend(unset_edits)
        if diff:
            log.append({
                "kind": "patch",
                "id": eid,
                "data": diff,
                "ok": True,
                "origin": "studio",
                "made_at": _now,
            })
        # Surface a per-slot clothing on/off tweak as a narrator block at
        # the top of the response (like the outfit swap), in addition to
        # the edit chip. Accumulates onto one clothing entry per character.
        co = (diff.get("properties") or {}).get("clothing_overrides") if isinstance(diff, dict) else None
        if isinstance(co, dict) and co:
            _upsert_clothing_narrator_state(conv, leaf, eid, co)
        convs.save_conversation(conv)
    eff_after = effective_entities_at(conv, leaf_id)
    return jsonify(eff_after.get(eid) or payload)


def _upsert_clothing_narrator_state(conv: dict, leaf: dict, eid: str, changes: dict) -> None:
    """Add/merge a ``clothing`` entry into the leaf's ``narrator_state``
    so a per-slot on/off toggle renders as a narrator block at the top of
    the response. ``narrator_state`` is normalized to a LIST so a clothing
    block can coexist with an outfit/move block on the same message."""
    meta = leaf.setdefault("metadata", {})
    ns = meta.get("narrator_state")
    items = ns if isinstance(ns, list) else ([ns] if isinstance(ns, dict) else [])
    entry = next(
        (it for it in items
         if isinstance(it, dict) and it.get("kind") == "clothing" and it.get("character") == eid),
        None,
    )
    if entry is None:
        char = ent.load_instance_entity(conv["id"], eid) or ent.get(eid)
        entry = {
            "kind": "clothing", "character": eid,
            "character_name": (char or {}).get("name") or eid, "slots": {},
        }
        items.append(entry)
    slots = entry.setdefault("slots", {})
    for k, v in changes.items():
        try:
            slots[str(k).lower()] = int(v)
        except (TypeError, ValueError):
            continue
    meta["narrator_state"] = items


# Display metadata carried from the leaf onto its branched sibling so the edit
# is NON-INTRUSIVE — the branched turn looks identical to the original (same
# picked image, etc.) rather than losing it. Excludes branch-identity / applied
# state keys (move/outfit/narrator_*/pending_edits/multi_response/applied_edits)
# which must not be duplicated onto the new sibling.
_BRANCH_CARRY_META_KEYS = ("image_pack_pick", "phrase_hits")


def surviving_cast_edits(leaf: dict | None) -> list[dict]:
    """The declarative cast-membership edits (``cast_add`` / ``cast_remove``) on
    ``leaf`` that must ride onto a branched sibling. Unlike move/outfit DELTAS,
    these are idempotent set-membership ops — a plain branch drops them, which is
    exactly the bug where a character removed on a turn silently returns the moment
    that turn is regenerated or a move/outfit branches it. Re-asserting them keeps a
    temp add / a removal sticky, while staying branch-scoped (they're log entries,
    not baseline mutations)."""
    log = ((leaf or {}).get("metadata") or {}).get("applied_edits") or []
    return [dict(e) for e in log
            if isinstance(e, dict) and e.get("kind") in ("cast_add", "cast_remove") and e.get("id")]


def _branch_newest(conv, leaf):
    """Resolve the parent + cloned fields for branching the newest message.

    Returns (parent_id, content, persona, speaker_id, carry_metadata). When the
    leaf has a parent, the new message becomes a SIBLING of the leaf that
    re-uses the leaf's content/persona/speaker AND its display metadata (the
    picked image) — so a state change (move/outfit) folds into the newest turn
    as a non-intrusive alternate branch the model can still respond to, rather
    than a standalone empty beat that drops the image. A root leaf can't be
    branched cleanly, so we fall back to appending a child of it.
    """
    if isinstance(leaf, dict) and leaf.get("parent_id"):
        src_meta = leaf.get("metadata") or {}
        carry = {k: src_meta[k] for k in _BRANCH_CARRY_META_KEYS if k in src_meta}
        return (
            leaf["parent_id"],
            leaf.get("content") or "",
            leaf.get("persona") or "narrator",
            leaf.get("speaker_id"),
            carry,
            surviving_cast_edits(leaf),
        )
    return (conv["active_path_leaf"], "", "narrator", None, {}, [])


@bp.post("/conversations/<cid>/move")
@login_required
def move_character(cid: str):
    """Append a narrator message describing a character's room change.

    The new message carries an updated presence_snapshot — the active leaf's
    snapshot with the character's location/room overridden — so subsequent
    prompts pick up the new scene. Pass room_id=null to clear the
    character's location entirely (they're "not present anywhere"); the
    presence map drops their location/room keys.
    """
    payload = request.get_json(silent=True) or {}
    char_id = payload.get("character_id")
    room_id = payload.get("room_id")  # may be None: "remove from any room"
    location_id = payload.get("location_id")
    if not char_id:
        return jsonify({"error": "character_id required"}), 400

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404

    # "Move to any room": a room that only exists as a global template
    # is instanced into the conversation (branch-scoped cast_add on the
    # active leaf) so the prompt's surroundings block can render it.
    # add_to_conversation_cast saves the conversation itself, so reload
    # before mutating below.
    if room_id and ent.load_instance_entity(cid, room_id) is None and ent.get(room_id):
        from .. import layers
        try:
            layers.add_to_conversation_cast(cid, room_id)
            conv = convs.load_conversation(cid)
        except ValueError:
            pass
    # Fill in the owning location when the client couldn't discover it
    # (global-only rooms aren't in its entity mirror yet).
    if room_id and not location_id:
        room_ent = ent.load_instance_entity(cid, room_id) or ent.get(room_id) or {}
        location_id = room_ent.get("location") or None
        if not location_id:
            for loc in ent.by_type("location"):
                if room_id in (loc.get("children") or []):
                    location_id = loc.get("id")
                    break

    leaf = conv["messages"][conv["active_path_leaf"]]
    parent_snap = leaf.get("presence_snapshot") or {}
    presence = dict(parent_snap.get("presence") or {})
    prev = dict(presence.get(char_id) or {})
    new_presence = dict(prev)
    if room_id is None:
        new_presence.pop("room", None)
        new_presence.pop("location", None)
    else:
        new_presence["room"] = room_id
        if location_id:
            new_presence["location"] = location_id
    presence[char_id] = new_presence

    # Follower sweep + path. Characters whose properties.following chains
    # up to the mover come along to the new room (transitive, cycle-guarded,
    # present-only); the shortest exit-path from the old room is recorded so
    # the next turn can narrate the journey. All pure state — no model call,
    # so no added response latency. Best-effort: a failure here must not
    # break an ordinary move.
    followed_moves: list[dict[str, Any]] = []
    walk_path: list[str] | None = None
    follow_applied_log: list[dict[str, Any]] = []
    if room_id is not None:
        try:
            from .. import mapnav
            from ..effective import effective_entities_at as _eff_at
            from ..narrator_apply import apply_edits as _apply_edits_fn
            eff_ents = _eff_at(conv, leaf["id"])
            # Gate raw presence by the branch's effective cast: a cut
            # character can linger as a phantom presence row (e.g. from a
            # narrator cast_remove that didn't rebuild the snapshot), and
            # without this filter the follower sweep / follow re-assert
            # below would drag that phantom along on every move.
            _cast_ids = set(effective_cast_at(conv, leaf["id"]).get("characters") or ())
            present_ids = {
                k for k, v in presence.items()
                if isinstance(v, dict) and (k == "user" or k in _cast_ids)
            }
            for fid in mapnav.follower_ids(eff_ents, char_id, present_ids):
                if fid == "user":
                    continue
                fprev = dict(presence.get(fid) or {})
                if fprev.get("room") == room_id:
                    continue  # already in the destination
                fnew = dict(fprev)
                fnew["room"] = room_id
                if location_id:
                    fnew["location"] = location_id
                presence[fid] = fnew
                fent = eff_ents.get(fid) or {}
                followed_moves.append({"character": fid, "name": fent.get("name") or fid})
            walk_path = mapnav.shortest_path(cid, prev.get("room"), room_id)
            # Re-assert every present character's `following` onto this new
            # branch. The move branches the newest message as a sibling, which
            # drops that message's applied_edits — so a follow set on the last
            # turn would silently vanish after one move. Replaying it here (a
            # log entry only; no baseline mutation, so it stays branch-scoped)
            # keeps following sticky. Idempotent — re-asserts the same value.
            reassert = [
                {"kind": "patch", "id": pid,
                 "data": {"properties": {"following": fv}}}
                for pid in present_ids
                if isinstance(
                    (fv := ((eff_ents.get(pid) or {}).get("properties") or {}).get("following")),
                    str) and fv
            ]
            if reassert:
                _, follow_applied_log = _apply_edits_fn(cid, reassert, parent_snap)
        except Exception:
            followed_moves = []
            walk_path = None
            follow_applied_log = []

    new_snap = {**parent_snap, "presence": presence}

    char = ent.load_instance_entity(cid, char_id) or ent.get(char_id) or {}
    char_name = char.get("name") or char_id
    prev_room_id = prev.get("room")
    prev_room = ent.load_instance_entity(cid, prev_room_id) if prev_room_id else None
    prev_room_name = (prev_room or {}).get("name") if prev_room else None

    if room_id is None:
        to_name = None  # stepped out of view
    else:
        new_room = ent.load_instance_entity(cid, room_id) or ent.get(room_id) or {}
        to_name = new_room.get("name") or room_id

    # Branch the newest message instead of appending a standalone beat: the new
    # sibling keeps the original turn's content/persona so the model still has
    # the real message to respond to (not just an empty move turn), and carries
    # the move as a narrator-edit chip + the updated presence. The sibling chip
    # flips between the pre- and post-move versions. Stored as structured state
    # (no prose) and excluded from the model prompt — the new location reaches
    # the model via presence_snapshot. Falls back to a child append when the
    # leaf is a root (a root can't be branched cleanly).
    branch_parent, clone_content, clone_persona, clone_speaker, carry_meta, carried_cast = _branch_newest(
        conv, leaf
    )
    msg = convs.append_message(
        conv,
        parent_id=branch_parent,
        persona=clone_persona,
        content=clone_content,
        speaker_id=clone_speaker,
        presence_snapshot=new_snap,
        metadata={
            **carry_meta,
            "narrator_state": {
                "kind": "move",
                "character": char_id,
                "character_name": char_name,
                "from": prev_room_name,
                "to": to_name,
                # Names of followers dragged along + the room chain walked,
                # so the UI chip / prompt can note "you passed through the
                # foyer" and "Serena came with you".
                "followers": [f["name"] for f in followed_moves] or None,
                "via": _path_room_names(cid, walk_path) if walk_path and len(walk_path) > 2 else None,
            },
            "move": {
                "character": char_id, "room": room_id, "location": location_id,
                "followers": followed_moves or None,
                "path": walk_path or None,
            },
            # Replayed edits that keep `following` sticky AND any cast add/remove
            # from the branched turn sticky (see the follower sweep + surviving_cast_
            # edits above). Empty for ordinary moves with a fresh cast.
            **({"applied_edits": carried_cast + follow_applied_log}
               if (carried_cast or follow_applied_log) else {}),
        },
    )
    convs.save_conversation(conv)
    return jsonify({"message": msg, "active_path_leaf": conv["active_path_leaf"]})


def _path_room_names(cid: str, path: list[str] | None) -> list[str] | None:
    """Resolve a room-id path to the display names of the *intermediate*
    rooms (drops the origin and destination), for a "you passed through …"
    note. None when there are no in-between rooms."""
    if not path or len(path) <= 2:
        return None
    names: list[str] = []
    for rid in path[1:-1]:
        r = ent.load_instance_entity(cid, rid) or ent.get(rid) or {}
        names.append(r.get("name") or rid)
    return names or None


@bp.get("/conversations/<cid>/map")
@login_required
def conversation_map(cid: str):
    """Return the navigable map for the map panel: locations (scenario-only
    by default, or all), each with its rooms — tags, exits, who's present,
    and hop-distance from the user's current room. Pure reads; no model
    call. ``?scope=all`` widens from the scenario's locations to every
    location in the catalog."""
    from .. import mapnav
    from ..effective import effective_entities_at as _eff_at

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    scope = (request.args.get("scope") or "scenario").strip().lower()

    leaf_id = conv.get("active_path_leaf") or ""
    leaf = (conv.get("messages") or {}).get(leaf_id) or {}
    presence = (leaf.get("presence_snapshot") or {}).get("presence") or {}
    eff = _eff_at(conv, leaf_id) if leaf_id else {}

    user_pres = presence.get("user") or {}
    current_room = user_pres.get("room")
    current_location = user_pres.get("location")
    dist = mapnav.distances_from(cid, current_room)

    # present cast grouped by room id → [{id, name, following}]
    present_by_room: dict[str, list[dict[str, Any]]] = {}
    for pid, info in presence.items():
        if not isinstance(info, dict):
            continue
        rid = info.get("room")
        if not rid:
            continue
        e = eff.get(pid) or ent.load_instance_entity(cid, pid) or ent.get(pid) or {}
        present_by_room.setdefault(rid, []).append({
            "id": pid,
            "name": ("You" if pid == "user" else (e.get("name") or pid)),
            "following": (e.get("properties") or {}).get("following") or None,
        })

    # Which locations to surface.
    if scope == "all":
        loc_ids = [l.get("id") for l in ent.by_type("location") if l.get("id")]
    else:
        scen = ent.get(conv.get("scenario_id") or "") or {}
        loc_ids = list(scen.get("locations") or [])
    # Always include the location the user is actually standing in.
    if current_location and current_location not in loc_ids:
        loc_ids.insert(0, current_location)

    def _resolve(eid: str) -> dict[str, Any]:
        return ent.load_instance_entity(cid, eid) or ent.get(eid) or {}

    locations_out: list[dict[str, Any]] = []
    seen_loc: set[str] = set()
    for lid in loc_ids:
        if not lid or lid in seen_loc:
            continue
        seen_loc.add(lid)
        loc = _resolve(lid)
        if not loc:
            continue
        rooms_out: list[dict[str, Any]] = []
        for rid in loc.get("children") or []:
            room = _resolve(rid)
            if not room or room.get("type") != "room":
                continue
            # A hidden room stays off the map until it's been discovered (a reveal
            # clears properties.hidden). The room you're standing in always shows.
            if (room.get("properties") or {}).get("hidden") and rid != current_room:
                continue
            rooms_out.append({
                "id": rid,
                "name": room.get("name") or rid,
                "tags": room.get("tags") or [],
                "exits": mapnav.room_exits(room),
                "locked": mapnav.locked_exits(room),   # barred doors: {dest: {reason, skill, dc}}
                "present": present_by_room.get(rid) or [],
                "distance": dist.get(rid),
                "reachable": rid in dist,
                "is_current": rid == current_room,
            })
        locations_out.append({
            "id": lid,
            "name": loc.get("name") or lid,
            "tags": loc.get("tags") or [],
            "rooms": rooms_out,
        })

    return jsonify({
        "scope": "all" if scope == "all" else "scenario",
        "current_room": current_room,
        "current_location": current_location,
        "locations": locations_out,
    })


@bp.post("/conversations/<cid>/follow")
@login_required
def set_following(cid: str):
    """Set or clear a character's ``properties.following`` via a replayed
    patch edit hung off the active leaf. Body: ``{character_id, follow}``
    where ``follow`` is a target character id (or null/"" to stop
    following). Branch-correct and reload-safe like every other edit."""
    from ..narrator_apply import apply_edits as _apply_edits
    from ..effective import effective_cast_at as _cast_at

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    char_id = payload.get("character_id")
    follow = payload.get("follow")
    if not isinstance(char_id, str) or not char_id:
        return jsonify({"error": "character_id required"}), 400
    follow_val = follow.strip() if isinstance(follow, str) and follow.strip() else None
    if follow_val == char_id:
        return jsonify({"error": "a character cannot follow itself"}), 400

    leaf_id = conv.get("active_path_leaf") or ""
    leaf = (conv.get("messages") or {}).get(leaf_id) or {}
    parent_snap = leaf.get("presence_snapshot") or {}
    existing_cast = _cast_at(conv, leaf_id).get("characters") or set()
    edits = [{
        "kind": "patch",
        "id": char_id,
        "data": {"properties": {"following": follow_val}},
    }]
    _, applied_log = _apply_edits(
        cid, edits, parent_snap, existing_cast_chars=existing_cast,
    )
    char = ent.load_instance_entity(cid, char_id) or ent.get(char_id) or {}
    char_name = char.get("name") or char_id
    tgt = ent.load_instance_entity(cid, follow_val) if follow_val else None
    tgt_name = ("you" if follow_val == "user" else ((tgt or {}).get("name") or follow_val)) if follow_val else None
    note = (f"[{char_name} is now following {tgt_name}]" if follow_val
            else f"[{char_name} stopped following]")
    msg = convs.append_message(
        conv, parent_id=leaf_id or None, persona="narrator",
        content=note, speaker_id=None,
        metadata={"applied_edits": applied_log,
                  "following_change": {"character": char_id, "follow": follow_val}},
    )
    convs.save_conversation(conv)
    return jsonify({"message": msg, "following": follow_val})


@bp.post("/conversations/<cid>/outfit")
@login_required
def change_outfit(cid: str):
    """Append a narrator message describing a character's outfit swap.

    Mutates the per-conversation instance entity's properties.current_outfit
    (so prompts that read the live entity see the new outfit too) and
    writes the new outfit onto the message's presence_snapshot — same
    shape narrator-edit's [outfit ...] directive produces.
    """
    payload = request.get_json(silent=True) or {}
    char_id = payload.get("character_id")
    outfit_id = payload.get("outfit_id")
    if not char_id or not outfit_id:
        return jsonify({"error": "character_id and outfit_id required"}), 400

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404

    char = ent.load_instance_entity(cid, char_id) or ent.get(char_id)
    if not char or char.get("type") != "character":
        return jsonify({"error": f"{char_id} not a character"}), 400

    # Pull the outfit template into the instance dir if it isn't already,
    # so subsequent prompt assembly resolves it locally.
    outfit = ent.load_instance_entity(cid, outfit_id) or ent.get(outfit_id)
    if not outfit or outfit.get("type") != "outfit":
        return jsonify({"error": f"outfit {outfit_id} not found"}), 400
    if not ent.load_instance_entity(cid, outfit_id):
        import copy as _copy
        from ..storage import write_json as _wj
        inst = _copy.deepcopy(outfit)
        inst["_template_id"] = outfit_id
        _wj(ent.instance_entities_dir(cid) / f"{outfit_id}.json", inst)

    # Apply the swap to the ACTIVE LEAF (the newest response) as branch-
    # scoped applied-edits — the same replay path as the narrator's
    # [outfit] directive and the per-slot clothing toggles — so it's
    # isolated to this branch AND the response's composed sprite reflects
    # it (effective_entities_at replays these onto the leaf, and
    # _resolve_sprite_state computes state at the message id). Previously
    # this branched a cloned message and mutated the baseline, which left
    # the on-screen image unchanged until the next generated turn.
    leaf_id = conv.get("active_path_leaf") or ""
    leaf = conv["messages"].get(leaf_id)
    if not leaf:
        return jsonify({"error": "no active leaf"}), 400

    char_name = char.get("name") or char_id
    outfit_name = outfit.get("name") or outfit_id

    meta = leaf.setdefault("metadata", {})
    log = meta.setdefault("applied_edits", [])
    if not isinstance(log, list):
        log = meta["applied_edits"] = []
    # Outfit edit first (fresh-slate: clears prior clothing_overrides /
    # transparency and the old worn map), then a worn patch repopulating
    # from the new bundle's equips so resolve_sprite_slots_v2 renders it.
    _now = int(time.time())
    log.append({
        "kind": "outfit", "character_id": char_id,
        "outfit_id": outfit_id, "ok": True, "origin": "studio",
        "made_at": _now,
    })
    equips = (outfit.get("properties") or {}).get("equips")
    if isinstance(equips, dict):
        def _default_state(pid: str) -> str:
            # A piece's default state is states[0], not the literal "on"
            # (some pieces are ["intact","ripped","off"] etc.); using "on"
            # would render those slots unclothed.
            piece = ent.get(pid)
            states = (piece.get("properties") or {}).get("states") if piece else None
            return states[0] if isinstance(states, list) and states and isinstance(states[0], str) else "on"
        new_worn = {
            slot: {"piece": piece_id, "state": _default_state(piece_id)}
            for slot, piece_id in equips.items()
            if isinstance(slot, str) and isinstance(piece_id, str)
        }
        log.append({
            "kind": "patch", "id": char_id,
            "data": {"properties": {"worn": new_worn}},
            "ok": True, "origin": "studio", "made_at": _now,
        })
    # Structured narrator block, rendered collapsed at the top of the
    # response (excluded from the model prompt — content stays empty).
    meta["narrator_state"] = {
        "kind": "outfit", "character": char_id,
        "character_name": char_name, "outfit": outfit_name,
    }
    # Reflect the outfit on the response's presence snapshot too.
    snap = leaf.get("presence_snapshot") or {}
    presence = dict(snap.get("presence") or {})
    new_presence = dict(presence.get(char_id) or {})
    new_presence["outfit"] = outfit_id
    presence[char_id] = new_presence
    leaf["presence_snapshot"] = {**snap, "presence": presence}
    leaf["edited_at"] = int(__import__("time").time())

    convs.save_conversation(conv)
    return jsonify({"message": leaf, "active_path_leaf": leaf_id})


@bp.post("/conversations/<cid>/outfit-profile")
@login_required
def set_outfit_profile(cid: str):
    """Pin (or clear) the active outfit profile for a tagged-format
    character. Narrows the image_pack_pick catalog to entries whose
    captions carry the profile's required tags so the per-turn image
    pick stops drifting between e.g. stage and bikini variants.

    Body: {"character_id": <id>, "profile_id": <id> | null}.

    Pure state pin — does NOT append a narrator message (the profile is
    a UI filter, not an in-fiction wardrobe change). Combined-format
    characters use `/conversations/<cid>/outfit` instead.
    """
    payload = request.get_json(silent=True) or {}
    char_id = payload.get("character_id")
    profile_id = payload.get("profile_id") or None
    if not char_id:
        return jsonify({"error": "character_id required"}), 400

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404

    char = ent.load_instance_entity(cid, char_id) or ent.get(char_id)
    if not char or char.get("type") != "character":
        return jsonify({"error": f"{char_id} not a character"}), 400

    profiles = sprite.outfit_profiles_of(char)
    if profile_id and profile_id not in profiles:
        return jsonify({"error": f"profile {profile_id} not declared on {char_id}"}), 400

    if profile_id:
        ent.apply_patch(cid, char_id, {"properties": {"current_outfit_profile": profile_id}})
    else:
        # Clear: load, drop the field, re-save (apply_patch is shallow merge,
        # can't unset).
        instance = ent.load_instance_entity(cid, char_id)
        if instance and isinstance(instance.get("properties"), dict):
            instance["properties"].pop("current_outfit_profile", None)
            ent.save_instance_entity(cid, instance)

    return jsonify({"character_id": char_id, "profile_id": profile_id})


@bp.post("/conversations/<cid>/user-persona")
@login_required
def set_user_persona(cid: str):
    """Pick (or clear) the user-persona character for this conversation.

    Body: {"card_id": <character_id> | null}.

    The conversation always has a `user` instance entity (created at
    conversation-creation time as a stub). When a card_id is given, the
    template (must be tagged "user") is deep-copied INTO the existing
    `user` entity — so narrator directives that reference `user`
    ([move user -> X], [outfit user -> Y], [set user.body_parts.head =
    ...]) target a real, fully-formed entity. The original card_id
    template is NOT cast into the conversation under its own id; the
    persona is exclusively the `user` entity.

    Passing card_id=null resets the `user` entity back to the empty
    stub so {{user}} expansion goes back to the default placeholder.
    """
    payload = request.get_json(silent=True) or {}
    card_id = payload.get("card_id") or None

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404

    template = None
    if card_id:
        template = ent.get(card_id)
        if not template or template.get("type") != "character":
            return jsonify({"error": f"character {card_id!r} not found"}), 404
        if "user" not in (template.get("tags") or []):
            return jsonify({"error": f"{card_id!r} is not tagged 'user'"}), 400

    # Build the new `user` entity. Always id="user" / type="character" so
    # narrator + path-replay treat it like any other character.
    if template:
        user_entity = copy.deepcopy(template)
        user_entity["id"] = "user"
        user_entity["type"] = "character"
        # Preserve the source template id for the studio's "edit
        # template" path and debugging.
        user_entity["_template_id"] = card_id
        tags = list(user_entity.get("tags") or [])
        if "user" not in tags:
            tags.append("user")
        user_entity["tags"] = tags
    else:
        user_entity = {
            "id": "user",
            "type": "character",
            "name": "User",
            "description": "",
            "tags": ["user"],
            "children": [],
            "properties": {},
            "example_text": "",
        }
    ent.save_instance_entity(cid, user_entity)

    persona = {
        "name": user_entity.get("name") or "User",
        "description": user_entity.get("description") or "",
        "card_id": card_id,
    }
    conv.setdefault("settings", {})["user_persona"] = persona
    convs.save_conversation(conv)

    instance = ent.load_instance_entities(cid)
    return jsonify({
        "settings": conv["settings"],
        "entities": instance,
        "active_path_leaf": conv["active_path_leaf"],
    })


@bp.post("/conversations/<cid>/messages/<mid>/revert-edit")
@login_required
def revert_applied_edit(cid: str, mid: str):
    """Mark a single applied narrator edit as reverted.

    Body: {"index": <int>} — position in `metadata.applied_edits`.
    With path-based effective state, "revert" is just dropping the
    entry from the replayed log: we set `reverted_at` on the entry and
    `effective_*` skips it. For move/outfit we also walk back the
    `presence_snapshot` on this message + descendants so the per-
    message snapshot reflects the rolled-back position; the rest of
    the state (entity properties, user persona) is recomputed at
    render time and needs no patching here.
    """
    payload = request.get_json(silent=True) or {}
    idx = payload.get("index")
    if not isinstance(idx, int):
        return jsonify({"error": "index required"}), 400

    conv = convs.load_conversation(cid)
    if not conv or mid not in conv["messages"]:
        return jsonify({"error": "message not found"}), 404
    msg = conv["messages"][mid]
    applied = (msg.get("metadata") or {}).get("applied_edits") or []
    if idx < 0 or idx >= len(applied):
        return jsonify({"error": "index out of range"}), 400

    entry = applied[idx]

    if entry.get("kind") in ("move", "outfit"):
        char_id = entry.get("character_id")
        before = entry.get("before") or {}
        for m in conv["messages"].values():
            snap = m.get("presence_snapshot") or {}
            presence = dict(snap.get("presence") or {})
            cur = dict(presence.get(char_id) or {})
            for k, v in before.items():
                if v is not None:
                    cur[k] = v
                else:
                    cur.pop(k, None)
            presence[char_id] = cur
            m["presence_snapshot"] = {**snap, "presence": presence}

    entry["reverted_at"] = int(__import__("time").time())
    convs.save_conversation(conv)

    return jsonify({"ok": True, "message": msg})


@bp.get("/conversations/<cid>/entities")
@login_required
def list_instance_entities(cid: str):
    """Return all entities for this conversation, keyed by id, with the
    active branch's narrator overlays applied.

    `effective_entities_at` walks root→leaf and replays every
    `applied_edits` entry onto a deep-copy of the disk baseline. Callers
    (chat.js' `state.entities` mirror after a branch switch, side-panel
    refreshes after narrator-add) get the same view the cast widget
    sees, so a `[set guy_1.name = "Kenji"]` overlay surfaces everywhere
    the client renders entity data.

    Optional ``?leaf=<mid>`` pins the replay to a specific path leaf;
    omitted, the conversation's `active_path_leaf` is used. The disk
    baseline is unchanged either way.
    """
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"entities": ent.load_instance_entities(cid)})
    leaf = request.args.get("leaf") or conv.get("active_path_leaf") or None
    return jsonify({"entities": effective_entities_at(conv, leaf)})


# ---------------------------------------------------------------------------
# Layered editor: effective view + save-at-layer + cast add/remove
# ---------------------------------------------------------------------------


@bp.get("/effective/<entity_id>")
@login_required
def get_effective(entity_id: str):
    """Return the merged view of an entity given an optional context.

    Query params:
      scenario      scenario id (apply scenario overrides)
      conversation  conversation id (apply instance overrides)

    The response contains the merged entity plus `_origin` (per-leaf layer
    map) and `_layers_present` (which layers contributed)."""
    from .. import layers
    sid = request.args.get("scenario") or None
    cid = request.args.get("conversation") or None
    eff = layers.effective_entity(entity_id, scenario_id=sid, conversation_id=cid)
    if eff is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(eff)


@bp.put("/effective/<entity_id>")
@login_required
def put_effective(entity_id: str):
    """Save a value at a specific layer.

    Query params:
      layer         one of template / scenario / instance (required)
      scenario      scenario id (required when layer=scenario)
      conversation  conversation id (required when layer=instance)

    Body: the full entity to persist. For the scenario layer we store the
    minimal diff against the template. For instance, the full entity."""
    from .. import layers
    layer = request.args.get("layer")
    if not layer:
        return jsonify({"error": "layer required"}), 400
    sid = request.args.get("scenario") or None
    cid = request.args.get("conversation") or None
    payload = request.get_json(silent=True) or {}
    try:
        eff = layers.save_at_layer(
            entity_id, payload, layer=layer, scenario_id=sid, conversation_id=cid
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(eff)


@bp.post("/scenarios/<sid>/cast/<character_id>")
@login_required
def add_scenario_cast(sid: str, character_id: str):
    from .. import layers
    try:
        scen = layers.add_to_scenario_cast(sid, character_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify(scen)


@bp.delete("/scenarios/<sid>/cast/<character_id>")
@login_required
def remove_scenario_cast(sid: str, character_id: str):
    from .. import layers
    try:
        scen = layers.remove_from_scenario_cast(sid, character_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify(scen)


@bp.post("/conversations/<cid>/cast/<character_id>")
@login_required
def add_conversation_cast(cid: str, character_id: str):
    from .. import layers
    payload = request.get_json(silent=True) or {}
    try:
        instance = layers.add_to_conversation_cast(
            cid, character_id,
            location_id=payload.get("location_id"),
            room_id=payload.get("room_id"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"character": instance})


@bp.delete("/conversations/<cid>/cast/<character_id>")
@login_required
def remove_conversation_cast(cid: str, character_id: str):
    from .. import layers
    existed = layers.remove_from_conversation_cast(cid, character_id)
    return jsonify({"removed": existed})


@bp.post("/conversations/<cid>/swap-outfit")
@login_required
def swap_outfit(cid: str):
    """Set a character's current_outfit, instancing the outfit template if
    it isn't already present in this conversation's instance."""
    import copy as _copy
    payload = request.get_json(silent=True) or {}
    char_id = payload.get("character_id")
    outfit_id = payload.get("outfit_id")
    if not char_id or not outfit_id:
        return jsonify({"error": "character_id and outfit_id required"}), 400
    char = ent.load_instance_entity(cid, char_id)
    if not char or char.get("type") != "character":
        return jsonify({"error": f"character {char_id!r} not in this conversation"}), 404

    instance_outfit = ent.load_instance_entity(cid, outfit_id)
    if not instance_outfit:
        template = ent.get(outfit_id)
        if not template or template.get("type") != "outfit":
            return jsonify({"error": f"outfit template {outfit_id!r} not found"}), 404
        instance_outfit = _copy.deepcopy(template)
        instance_outfit["_template_id"] = outfit_id
        ent.save_instance_entity(cid, instance_outfit)

    char.setdefault("properties", {})["current_outfit"] = outfit_id
    ent.save_instance_entity(cid, char)
    return jsonify({"character": char, "outfit": instance_outfit})


# ---------------------------------------------------------------------------
# Prompt viewer
# ---------------------------------------------------------------------------


@bp.get("/conversations/<cid>/prompt")
@login_required
def view_prompt(cid: str):
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    persona = request.args.get("persona", "narrator")
    speaker = request.args.get("speaker_id")
    try:
        prompt = assemble_prompt(conv, persona, speaker_id=speaker)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(prompt)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


@bp.get("/ollama/models")
@login_required
def ollama_models():
    return jsonify({"models": model_names()})


@bp.post("/ollama/test")
@login_required
def ollama_test():
    payload = request.get_json(silent=True) or {}
    return jsonify(
        test_connection(host=payload.get("host"), model=payload.get("model"))
    )


@bp.post("/ollama/warmup")
@login_required
def ollama_warmup():
    payload = request.get_json(silent=True) or {}
    return jsonify(warmup(model=payload.get("model"), host=payload.get("host")))


@bp.get("/ollama/loaded")
@login_required
def ollama_loaded():
    """Models currently resident in Ollama's memory."""
    return jsonify({"models": list_loaded()})


# ---------------------------------------------------------------------------
# App settings
# ---------------------------------------------------------------------------


@bp.get("/settings")
@login_required
def get_settings():
    """Return the user-editable slice of the loaded config."""
    cfg = current_app.config
    return jsonify(
        {
            "ollama": cfg.get("ollama") or {},
            "defaults": cfg.get("defaults") or {},
            "model_profiles": cfg.get("model_profiles") or {},
            "network": cfg.get("network") or {},
            "host": cfg.get("HOST"),
            "port": cfg.get("PORT"),
        }
    )


@bp.put("/settings")
@login_required
def put_settings():
    """Persist a partial settings update into config.local.json and reload."""
    payload = request.get_json(silent=True) or {}
    allowed = {k: payload[k] for k in ("ollama", "defaults", "model_profiles", "network") if k in payload}
    if not allowed:
        return jsonify({"error": "No editable keys in payload."}), 400
    fresh = save_local_overrides(allowed)
    return jsonify(
        {
            "ollama": fresh.get("ollama") or {},
            "defaults": fresh.get("defaults") or {},
            "model_profiles": fresh.get("model_profiles") or {},
            "network": fresh.get("network") or {},
            "host": fresh.get("HOST"),
            "port": fresh.get("PORT"),
        }
    )


# ---------------------------------------------------------------------------
# Per-model sampling profiles
# ---------------------------------------------------------------------------


@bp.get("/profiles")
@login_required
def list_profiles():
    return jsonify({"model_profiles": current_app.config.get("model_profiles") or {}})


@bp.get("/prompt-defaults")
@login_required
def prompt_defaults():
    """Return the built-in default system templates so the UI can show
    them as placeholders / reset targets."""
    from ..personas import DEFAULT_SYSTEM_CHARACTER, DEFAULT_SYSTEM_NARRATOR
    return jsonify({
        "system_prompt_character": DEFAULT_SYSTEM_CHARACTER,
        "system_prompt_narrator": DEFAULT_SYSTEM_NARRATOR,
    })


@bp.put("/profiles/<model>")
@login_required
def put_profile(model: str):
    """Replace the sampling profile for a model. Empty body removes it."""
    payload = request.get_json(silent=True) or {}
    profiles = dict(current_app.config.get("model_profiles") or {})
    cleaned = {k: v for k, v in payload.items() if v is not None and v != ""}
    if cleaned:
        profiles[model] = cleaned
    else:
        profiles.pop(model, None)
    fresh = save_local_overrides({"model_profiles": profiles})
    return jsonify({"model_profiles": fresh.get("model_profiles") or {}})


# ---------------------------------------------------------------------------
# Maintenance: pull origin + restart the server (Settings → Update & restart)
# ---------------------------------------------------------------------------
#
# A self-hosted convenience: one button pulls the latest from origin and
# restarts the process so new code takes effect. Data-only pulls (new
# characters/scenarios) are actually picked up live by the request-scoped /
# process entity cache, but per the UI contract this always restarts so the
# behaviour is predictable.
#
# Gated to loopback (or an explicitly allow-listed IP) on top of the global
# allowlist — running git and re-exec'ing the process must never be reachable
# from an untrusted client.


def _admin_caller_allowed() -> bool:
    """True for a loopback caller, an explicitly allow-listed IP, or — when the
    ``network.allow_remote_admin`` opt-in is set — any authenticated device (the
    endpoint is already ``@login_required``). The opt-in is what lets the
    Update & restart button work from a phone on the LAN."""
    net = current_app.config.get("network") or {}
    if net.get("allow_remote_admin"):
        return True
    ip = request.remote_addr or ""
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    return ip in list(net.get("allowed_ips") or [])


def _repo_root() -> Path:
    # api.py lives at <repo>/app/routes/api.py → parents[2] is <repo>.
    return Path(__file__).resolve().parents[2]


def _mark_fds_close_on_exec() -> None:
    """Make every inherited fd (except stdio) close on exec.

    Werkzeug's dev server marks its listening socket *inheritable* (so its
    auto-reloader can hand the socket to a child). Across our os.execv that
    means the new interpreter inherits the still-open listening socket and dies
    with "Address already in use" when it tries to re-bind the port. Setting the
    fds non-inheritable makes the kernel close them at exec, freeing the port.
    """
    fds: list[int] = []
    for path in ("/proc/self/fd", "/dev/fd"):   # Linux, then BSD/macOS
        try:
            fds = [int(x) for x in os.listdir(path)]
            break
        except OSError:
            continue
    if not fds:
        try:
            import resource
            soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        except Exception:
            soft = 4096
        fds = list(range(3, min(int(soft), 65536)))
    for fd in fds:
        if fd > 2:
            try:
                os.set_inheritable(fd, False)
            except OSError:
                pass


# The launcher (start.sh / start.bat) relaunches the server when it exits with
# this code. That's the clean, cross-platform restart: the old process fully
# exits (freeing the port), then the launcher starts a fresh one on the pulled
# code. It avoids in-place os.execv, which on Windows can't replace a process —
# it spawns a detached copy and drops the .bat into its `pause`, orphaning things.
_RESTART_EXIT_CODE = 42


def _schedule_restart() -> None:
    """Restart the server shortly after the response flushes.

    Preferred path: exit with ``_RESTART_EXIT_CODE`` so the loop launcher
    (which sets ``GEMMASIM_LAUNCHER=1``) relaunches us. If there's no launcher
    (someone ran ``python run.py`` directly), fall back to an in-place os.execv,
    marking inherited fds close-on-exec first so the new process can re-bind the
    port. The short delay lets the HTTP response reach the browser first; the
    page then polls /api/admin/ping and reloads once the new process answers.
    """
    def _runner():
        time.sleep(0.8)
        if os.environ.get("GEMMASIM_LAUNCHER"):
            os._exit(_RESTART_EXIT_CODE)          # launcher relaunches us
        _mark_fds_close_on_exec()                 # so the new process can re-bind the port
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except Exception:
            os._exit(_RESTART_EXIT_CODE)

    threading.Thread(target=_runner, daemon=True).start()


@bp.route("/admin/ping")
def admin_ping():
    """Liveness probe the Update-&-restart button polls after a restart."""
    return jsonify({"ok": True})


@bp.route("/admin/pull_restart", methods=["POST"])
@login_required
def admin_pull_restart():
    """Pull origin/<current-branch> with --ff-only, then restart the server.

    Returns the git output; the restart is scheduled right after so the
    response still reaches the client. A failed pull is reported but the
    restart still happens (the button is, fundamentally, a restart button).
    """
    if not _admin_caller_allowed():
        return jsonify({
            "error": "remote_admin_disabled",
            "message": "Updating from another device is turned off. In Settings → "
                       "Update & restart, enable “Allow update from other devices”, then try again.",
        }), 403

    repo = str(_repo_root())
    result: dict = {"branch": None, "pull_ok": False, "output": "", "restarting": True}
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=15,
        ).stdout.strip() or "HEAD"
        result["branch"] = branch
        pull = subprocess.run(
            ["git", "pull", "--ff-only", "origin", branch],
            cwd=repo, capture_output=True, text=True, timeout=180,
        )
        result["pull_ok"] = pull.returncode == 0
        result["output"] = ((pull.stdout or "") + (pull.stderr or "")).strip()
    except Exception as e:
        result["output"] = f"git pull error: {e}"

    _schedule_restart()
    return jsonify(result)
