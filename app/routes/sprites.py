"""Local sprite image endpoint.

Serves composed character + scene PNGs at:

  GET /sprites/<sprite_id>/<slots>/<scene>
  GET /sprites/<sprite_id>/<slots>/<garments>/<scene>

`slots` is a comma-CSV of 8 ints (top, bottom, bra, panties, pantyhose,
gloves, legwear, shoes). `garments` (optional) is a parallel comma-CSV
of named garment ids per slot — ``-`` for slots that should use the
wardrobe's default garment. `scene` is one of the scene tags shipped
under ``<assets>/Scene/`` (Class, Hall, Library, …).

Compose pipeline reads ``<assets>/<sprite_id>/wardrobe.json`` (when
present) to translate (slots, garments) into layer PNGs; otherwise
falls back to the legacy hard-coded ``Image1.png … Image14.png``
catalog. Either way the composed PNG is alpha-blended over the scene
background and cached on disk by content hash.

Behind ``@login_required`` so the asset dir doesn't become a public
mirror.
"""
from __future__ import annotations

import logging

from flask import Blueprint, Response, abort, request, send_file
import io

from ..auth import login_required
from .. import sprite_compose as compose


bp = Blueprint("sprites", __name__)
log = logging.getLogger(__name__)


def _serve(sprite_id: str, slots: str, scene: str, garments: str | None):
    parsed = compose.parse_slots(slots)
    parsed_g = compose.parse_garments(garments) if garments else None
    # Per-layer transparency (0..100 each slot). Optional ?tr=...
    # query string. Solo-route always renders cropped to the center
    # third so a single character fills more of the viewport.
    tr_raw = request.args.get("tr") or ""
    parsed_tr = compose.parse_transparency(tr_raw) if tr_raw else None

    # Strong ETag derived from the deterministic cache key. The composed
    # PNG for a given (sprite, slots, garments, scene) tuple is
    # byte-stable forever, so a content-hash ETag lets a returning
    # client skip re-downloading the bytes via 304 Not Modified.
    try:
        etag = '"' + compose.cache_key_for(
            sprite_id, parsed, scene, parsed_g,
            crop_solo=True, transparency=parsed_tr,
        ) + '"'
    except Exception:
        etag = None
    inm = request.headers.get("If-None-Match")
    if etag and inm and etag in {t.strip() for t in inm.split(",")}:
        resp = Response(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "public, no-cache"
        return resp

    try:
        png = compose.compose_png(
            sprite_id, parsed, scene, parsed_g,
            crop_solo=True, transparency=parsed_tr,
        )
    except FileNotFoundError as e:
        log.info("sprite 404: %s", e)
        abort(404)
    resp = send_file(
        io.BytesIO(png),
        mimetype="image/png",
        max_age=0,
    )
    if etag:
        resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "public, no-cache"
    return resp


@bp.get("/<sprite_id>/<slots>/<scene>")
@login_required
def serve(sprite_id: str, slots: str, scene: str):
    return _serve(sprite_id, slots, scene, None)


@bp.get("/<sprite_id>/<slots>/<garments>/<scene>")
@login_required
def serve_with_garments(sprite_id: str, slots: str, garments: str, scene: str):
    return _serve(sprite_id, slots, scene, garments)


def _parse_group_spec(
    spec: str,
) -> tuple[str, tuple[int, ...], tuple[str, ...] | None, tuple[int, ...] | None]:
    """Parse a single ``<sprite_id>:<slots>[:<garments>[:<transparency>]]``
    segment into the tuple ``compose_multi_png`` expects, plus an
    optional per-slot transparency tuple (0..100)."""
    parts = spec.split(":")
    if not parts or not parts[0]:
        raise ValueError(f"empty group spec: {spec!r}")
    sprite_id = parts[0]
    slots = compose.parse_slots(parts[1] if len(parts) > 1 else "")
    garments = compose.parse_garments(parts[2]) if len(parts) > 2 and parts[2] else None
    transparency = compose.parse_transparency(parts[3]) if len(parts) > 3 and parts[3] else None
    return sprite_id, slots, garments, transparency


@bp.get("/group/<scene>/<path:specs>")
@login_required
def serve_group(scene: str, specs: str):
    """Side-by-side multi-character composite. Each ``/<spec>`` segment
    in the path encodes one participant; characters are pasted over the
    shared scene at column-center offsets by
    ``sprite_compose.compose_multi_png``.
    """
    try:
        full = [_parse_group_spec(s) for s in specs.split("/") if s]
    except ValueError as e:
        log.info("group sprite 400: %s", e)
        abort(400)
    if not full:
        abort(404)
    participants = [(sid, slots, g) for sid, slots, g, _ in full]
    transparency_per: list[tuple[int, ...] | None] = [tr for *_, tr in full]
    if all(t is None for t in transparency_per):
        transparency_per = None  # type: ignore[assignment]

    try:
        etag = '"' + compose.cache_key_for_multi(
            participants, scene, transparency_per=transparency_per,
        ) + '"'
    except Exception:
        etag = None
    inm = request.headers.get("If-None-Match")
    if etag and inm and etag in {t.strip() for t in inm.split(",")}:
        resp = Response(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "public, no-cache"
        return resp

    try:
        png = compose.compose_multi_png(
            participants, scene, transparency_per=transparency_per,
        )
    except FileNotFoundError as e:
        log.info("group sprite 404: %s", e)
        abort(404)
    resp = send_file(io.BytesIO(png), mimetype="image/png", max_age=0)
    if etag:
        resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "public, no-cache"
    return resp
