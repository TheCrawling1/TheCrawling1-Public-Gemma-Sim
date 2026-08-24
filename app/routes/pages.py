"""HTML page routes."""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, current_app, render_template, send_from_directory

from .. import conversations as convs
from .. import effective as eff
from .. import entities as ent
from ..auth import login_required


bp = Blueprint("pages", __name__)


@bp.route("/")
@login_required
def dashboard():
    return render_template(
        "dashboard.html",
        scenarios=ent.by_type("scenario"),
        conversations=convs.list_conversations(),
    )


@bp.route("/chat/<conversation_id>")
@login_required
def chat(conversation_id: str):
    conv = convs.load_conversation(conversation_id)
    if not conv:
        abort(404)
    # Path-effective view: cast widget, Speak-as / Reply-as dropdowns,
    # and every other client-side consumer of `state.entities` should
    # see narrator overlays (renames, body_parts patches, outfit swaps)
    # applied — not the disk baseline. The baseline still exists in
    # `instances/<cid>/entities/<eid>.json`, but it's just the seed
    # `effective_entities_at` replays the path onto.
    instance = eff.effective_entities_at(conv)
    # Expose only the config defaults the client actually reads, not the
    # whole config (which holds host bindings, IP allowlists, etc.).
    defaults = current_app.config.get("defaults") or {}
    client_config = {
        "defaults": {
            "image_pack_pick": bool(defaults.get("image_pack_pick", False)),
            "auto_state_changes": bool(defaults.get("auto_state_changes", False)),
            "auto_state_transparency": bool(defaults.get("auto_state_transparency", False)),
            "auto_state_location": bool(defaults.get("auto_state_location", False)),
        },
    }
    # Globally available user-persona character cards: any character
    # template whose tags include "user". Surfaced to the persona-editor
    # dialog so the user can pick one as their persona instead of typing
    # name/description from scratch.
    user_personas = [
        {
            "id": c.get("id"),
            "name": c.get("name") or c.get("id"),
            "description": c.get("description") or "",
        }
        for c in ent.by_type("character")
        if "user" in (c.get("tags") or [])
    ]
    # Globally available outfits for the cast rows' Clothing control.
    # Scenarios rarely seed enough outfits per-character, and the user
    # wants to be able to dress a character in anything in the library.
    # The /outfit endpoint auto-copies the template into the instance on
    # first use, so picking a global outfit just works. Each entry
    # carries the same shape the Scene-staging panel uses (is_accessory
    # / under / clothing_slots / partial_label / owner) so the cast
    # Clothing control can mirror staging: primary-outfit list, per-slot
    # toggles, and an accessories multi-select. The client builds each
    # character's catalog from this list (owned + generic) the same way
    # setups.outfits_for does server-side.
    def _outfit_slots(o):
        raw = (o.get("properties") or {}).get("clothing_slots") or {}
        slots = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if not isinstance(k, str):
                    continue
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    continue
                if n in (1, 2, 3):
                    slots[k.lower()] = n
        return slots

    global_outfits = []
    for o in ent.by_type("outfit"):
        props = o.get("properties") or {}
        owner = (props.get("owner") or "").strip().lower()
        global_outfits.append({
            "id": o.get("id"),
            "name": o.get("name") or o.get("id"),
            "owner": owner,
            "generic": owner in ("", "generic"),
            "is_accessory": bool(props.get("is_accessory")),
            "under": bool(props.get("under")),
            "clothing_slots": _outfit_slots(o),
            "partial_label": (props.get("partial_label") or None),
        })
    # Path-derived view: the cast set, persona, and last responder for
    # the active branch. Computed server-side so the initial Jinja
    # render and the JS state both start from the same value, and so
    # the chat.html template can pick the right persona dropdown
    # selection without consulting global settings.
    cast = eff.effective_cast_at(conv)
    effective_persona = eff.effective_user_persona(conv)
    default_responder = eff.default_responder_for_path(conv)
    setup_root = eff.active_setup_root_for_path(conv)
    active_setup_root_id = (setup_root or {}).get("id") or ""
    # Conversation-baseline cast: the instance scenario's birth-time
    # characters[]/objects[]. Used by the client to replay
    # cast_add/cast_remove edits along any leaf's path without a
    # server roundtrip — keeps branch-switch dropdown updates
    # synchronous instead of waiting on /active-leaf.
    scenario_id = conv.get("scenario_id") or ""
    inst_scenario = ent.load_instance_entity(conversation_id, scenario_id) if scenario_id else None
    scenario_baseline_cast = {
        "characters": list((inst_scenario or {}).get("characters") or []),
        # Objects have no present-baseline: scenario objects[] is the
        # staging pool, and an object is only in a branch's scene via a
        # path-replayed cast_add (mirrors effective_cast_at).
        "objects": [],
    }
    # Global object templates for the Objects block's add picker —
    # same rationale as global_outfits above: adding via the cast
    # endpoint auto-instances the template on first use.
    global_objects = [
        {"id": o.get("id"), "name": o.get("name") or o.get("id")}
        for o in ent.by_type("object")
    ]
    # Global room templates for the cast rows' Location combobox, so a
    # character can be moved to ANY room — not just the scenario's. The
    # client instances a non-instanced room through the cast endpoint
    # before committing the move (the prompt's surroundings block only
    # renders instanced rooms).
    global_rooms = [
        {"id": r.get("id"), "name": r.get("name") or r.get("id")}
        for r in ent.by_type("room")
    ]
    # Module manifests for the chat client. The toolbar autoplay
    # toggle + the left-panel modules section both consume these to
    # render controls; the active list for the current branch is read
    # off the setup root's metadata.
    from .. import modules as _modules_mod
    module_manifests = list(_modules_mod.all_manifests().values())

    # Asset descriptors per module — each entry carries the URLs of
    # the module's <id>.js and <id>.css files (when present on disk),
    # so chat.html can render <script> / <link> tags for them at page
    # load. Modules without a .js or .css ship with the corresponding
    # url as None and the template skips that tag.
    #
    # Active set: modules listed in the active setup root's
    # metadata.modules. Surfaced separately so chat.js can gate
    # window.Modules.isActive() and per-module JS init.
    import os
    data_dir = current_app.config.get("data_dir") or ""
    modules_dir = os.path.join(data_dir, "modules")
    module_assets: list[dict] = []
    for manifest in module_manifests:
        mid = manifest.get("id")
        if not mid:
            continue
        js_path = os.path.join(modules_dir, mid, f"{mid}.js")
        css_path = os.path.join(modules_dir, mid, f"{mid}.css")
        module_assets.append({
            "id": mid,
            "js_url": (f"/modules/{mid}/static/{mid}.js"
                       if os.path.isfile(js_path) else None),
            "css_url": (f"/modules/{mid}/static/{mid}.css"
                        if os.path.isfile(css_path) else None),
        })
    active_module_ids: list[str] = []
    if setup_root:
        raw = (setup_root.get("metadata") or {}).get("modules") or []
        if isinstance(raw, list):
            active_module_ids = [m for m in raw if isinstance(m, str)]

    # Prefab assets — same shape as module assets. A prefab's drop-in
    # renderer + styles live at data/prefabs/<id>/prefab.js / prefab.css;
    # chat.html loads them so a new kind's UI registers on window.Prefabs
    # without any engine edit. Prefabs without a .js/.css ship None and
    # the template skips that tag.
    from .. import prefabs as _prefabs_mod
    prefabs_dir = os.path.join(data_dir, "prefabs")
    prefab_assets: list[dict] = []
    for pid in _prefabs_mod.all_manifests().keys():
        if not pid:
            continue
        js_path = os.path.join(prefabs_dir, pid, "prefab.js")
        css_path = os.path.join(prefabs_dir, pid, "prefab.css")
        prefab_assets.append({
            "id": pid,
            "js_url": (f"/prefabs/{pid}/static/prefab.js"
                       if os.path.isfile(js_path) else None),
            "css_url": (f"/prefabs/{pid}/static/prefab.css"
                        if os.path.isfile(css_path) else None),
        })

    return render_template(
        "chat.html",
        conversation=conv,
        entities=instance,
        client_config=client_config,
        user_personas=user_personas,
        global_outfits=global_outfits,
        global_objects=global_objects,
        global_rooms=global_rooms,
        effective_cast={
            "characters": sorted(cast["characters"]),
            "objects": sorted(cast["objects"]),
        },
        effective_user_persona=effective_persona,
        default_responder=default_responder or "",
        active_setup_root_id=active_setup_root_id,
        scenario_baseline_cast=scenario_baseline_cast,
        module_manifests=module_manifests,
        module_assets=module_assets,
        active_module_ids=active_module_ids,
        prefab_assets=prefab_assets,
    )


@bp.route("/studio")
@login_required
def studio():
    return render_template(
        "studio.html",
        characters=ent.by_type("character"),
        locations=ent.by_type("location"),
        rooms=ent.by_type("room"),
        objects=ent.by_type("object"),
        outfits=ent.by_type("outfit"),
        # Only SHARED/generic pieces here — owner-scoped pieces (the vast
        # majority) are managed on their character's Clothing tab, not as
        # a flat global wall.
        clothing=[
            c for c in ent.by_type("clothing")
            if not (c.get("properties") or {}).get("owner")
        ],
        scenarios=ent.by_type("scenario"),
    )


_EDITOR_TEMPLATES = {
    "character": "studio_character.html",
    "location": "studio_location.html",
    "outfit": "studio_outfit.html",
    "clothing": "studio_clothing.html",
    "object": "studio_object.html",
    "scenario": "studio_scenario.html",
    "room": "studio_room.html",
}


def _editor_context() -> dict:
    """Reference data the editor pages need for dropdowns / pickers."""
    return {
        "all_characters": ent.by_type("character"),
        "all_locations": ent.by_type("location"),
        "all_rooms": ent.by_type("room"),
        "all_outfits": ent.by_type("outfit"),
        "all_clothing": ent.by_type("clothing"),
        "all_objects": ent.by_type("object"),
        "all_scenarios": ent.by_type("scenario"),
    }


@bp.route("/studio/<entity_type>/new")
@login_required
def studio_new(entity_type: str):
    if entity_type not in _EDITOR_TEMPLATES:
        abort(404)
    return render_template(
        _EDITOR_TEMPLATES[entity_type],
        entity=None,
        entity_type=entity_type,
        is_new=True,
        **_editor_context(),
    )


@bp.route("/studio/<entity_type>/<entity_id>")
@login_required
def studio_edit(entity_type: str, entity_id: str):
    if entity_type not in _EDITOR_TEMPLATES:
        abort(404)
    e = ent.get(entity_id)
    if not e or e.get("type") != entity_type:
        abort(404)
    return render_template(
        _EDITOR_TEMPLATES[entity_type],
        entity=e,
        entity_type=entity_type,
        is_new=False,
        **_editor_context(),
    )


@bp.route("/settings")
@login_required
def settings_page():
    return render_template("settings.html")


_FILE_ROOTS = ("data", "static", "instances")


@bp.route("/file/<path:rel>")
@login_required
def serve_file(rel: str):
    """Serve any file under data/, static/, or instances/. Lets messages
    embed `![alt](data/...)` markdown images that resolve to local paths
    without exposing arbitrary filesystem reads.
    """
    from ..config import PROJECT_ROOT

    candidate = (PROJECT_ROOT / rel).resolve()
    try:
        candidate.relative_to(PROJECT_ROOT)
    except ValueError:
        abort(403)
    parts = candidate.relative_to(PROJECT_ROOT).parts
    if not parts or parts[0] not in _FILE_ROOTS:
        abort(403)
    if not candidate.is_file():
        abort(404)
    return send_from_directory(candidate.parent, candidate.name, max_age=3600)


@bp.route("/portraits/<character_id>")
@login_required
def portrait(character_id: str):
    """Serve a character's portrait from data/characters/<id>/<filename>.

    Filename comes from the character entity's properties.portrait field;
    falls back to portrait.png. Always served from the template folder so
    instanced conversations don't need to duplicate the binary.
    """
    char = ent.get(character_id)
    if not char or char.get("type") != "character":
        abort(404)
    filename = (char.get("properties") or {}).get("portrait") or "portrait.png"
    folder = Path(current_app.config["data_dir"]) / "characters" / character_id
    if not (folder / filename).is_file():
        abort(404)
    return send_from_directory(folder, filename, max_age=3600)


@bp.route("/character_images/<character_id>/<path:filename>")
@login_required
def character_image(character_id: str, filename: str):
    """Serve a per-character catalog image from data/characters/<id>/images/.

    Companion to /portraits/<id> for tagged-format characters whose
    properties.images.entries reference local files instead of a CDN
    URL. send_from_directory handles path-traversal safety; we still
    do an existence check up front so missing files 404 cleanly
    instead of triggering its more generic handling.
    """
    char = ent.get(character_id)
    if not char or char.get("type") != "character":
        abort(404)
    folder = Path(current_app.config["data_dir"]) / "characters" / character_id / "images"
    if not (folder / filename).is_file():
        abort(404)
    return send_from_directory(folder, filename, max_age=3600)
