"""SSE streaming endpoint: token-by-token AI generation.

Client opens an EventSource on /api/conversations/<cid>/generate, the server
streams chunks as `data: {...}` events, and the final event includes the
persisted message id and any narrator-extracted edits.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Iterator

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from .. import conversations as convs
from .. import multi_response as mr
from .. import sprite_url as sprite
from ..auth import login_required
from ..effective import effective_entities_at
from ..entities import load_instance_entities
from ..narrator import extract_edits
from ..narrator_apply import apply_edits
from ..ollama_client import chat_stream, chat_sync
from ..personas import assemble_prompt, banned_phrase_hits
from ..prompt import run_message_annotators, run_output_filters


# Per-conversation locks so concurrent generations can't double-summarize
# or trample each other's settings.summary writes.
_summary_locks: dict[str, threading.Lock] = {}
# Per-(conversation, focal) locks for the memory extractor (a sibling of the
# summarizer — same truncation-boundary seam, but per-character facts).
_memory_locks: dict[str, threading.Lock] = {}


def _walk_downstream_in_group(
    conv: dict[str, Any], start_id: str, group_id: str
) -> list[dict[str, Any]]:
    """Walk the active chain downward from start_id, collecting every
    descendant that's still part of the same multi-response group_id.
    Stops at the first non-group child (e.g. a user message that
    follows the group). Honors branch_choices when a node has multiple
    children — the inactive sibling branch is left alone.
    """
    msgs = conv.get("messages") or {}
    branch_choices = conv.get("branch_choices") or {}
    children_by_parent: dict[str, list[str]] = {}
    for mid, m in msgs.items():
        children_by_parent.setdefault(m.get("parent_id") or "", []).append(mid)
    out: list[dict[str, Any]] = []
    cur = start_id
    while True:
        kids = children_by_parent.get(cur, [])
        if not kids:
            break
        if len(kids) == 1:
            next_id = kids[0]
        else:
            next_id = branch_choices.get(cur) or kids[0]
        nxt = msgs.get(next_id)
        if not nxt:
            break
        meta = (nxt.get("metadata") or {}).get("multi_response") or {}
        if meta.get("group_id") != group_id:
            break
        out.append(nxt)
        cur = next_id
    return out


def _carry_group_chain(
    cid: str,
    *,
    original_id: str,
    new_msg: dict[str, Any],
) -> list[dict[str, Any]]:
    """When regenerating a single member of a multi-response group, the
    rest of the chain (everything below the regenerated node) should
    follow the new sibling rather than be orphaned on the old branch.

    Stamps the new message with the same role/ordinal as the original
    (lead regen establishes a fresh group_id == new_msg.id), then walks
    the original's active downstream chain inside the same group and
    clones each member under the new message in order. Returns the
    cloned partner messages in arrival order.

    Cloning (not re-parenting) keeps the old branch intact — the
    original chain is still reachable via sibling chips, only the
    active path moves to the new branch.
    """
    live_conv = convs.load_conversation(cid)
    if not live_conv:
        return []
    msgs = live_conv.get("messages") or {}
    original = msgs.get(original_id)
    new_live = msgs.get(new_msg["id"]) if new_msg else None
    if not original or not new_live:
        return []
    grp = (original.get("metadata") or {}).get("multi_response") or {}
    group_id = grp.get("group_id")
    if not group_id:
        return []
    orig_role = grp.get("role", "partner")
    orig_ordinal = grp.get("ordinal", 0)

    new_group_id = new_msg["id"] if orig_role == "lead" else group_id

    # Stamp the new message so it occupies the same slot in the (new)
    # group as the original did.
    new_live.setdefault("metadata", {})["multi_response"] = {
        "group_id": new_group_id,
        "role": orig_role,
        "ordinal": orig_ordinal,
    }

    downstream = _walk_downstream_in_group(live_conv, original_id, group_id)
    out: list[dict[str, Any]] = []
    prev_id = new_msg["id"]
    for src in downstream:
        src_meta = (src.get("metadata") or {}).get("multi_response") or {}
        clone_meta = dict(src.get("metadata") or {})
        clone_meta["multi_response"] = {
            "group_id": new_group_id,
            "role": src_meta.get("role", "partner"),
            "ordinal": src_meta.get("ordinal", 0),
        }
        clone = convs.append_message(
            live_conv,
            parent_id=prev_id,
            persona=src.get("persona"),
            content=src.get("content") or "",
            speaker_id=src.get("speaker_id"),
            presence_snapshot=src.get("presence_snapshot"),
            metadata=clone_meta,
        )
        out.append(clone)
        prev_id = clone["id"]
    convs.save_conversation(live_conv)

    # Re-compute the side-by-side composite for the new branch — the
    # regenerated node and the cloned partners may carry different
    # outfits / scenes than the original chain, so the original group
    # image (cloned via metadata copy) is stale.
    try:
        if _stamp_group_image(cid, live_conv, new_group_id):
            convs.save_conversation(live_conv)
    except Exception:
        log.exception("carry-chain group image stamp failed cid=%s", cid)

    return out


def _multi_topup_targets(
    partner_names: list[str],
    bodies: dict[str, str],
    joint_truncated: bool,
) -> set[str]:
    """Partner display names whose beat must be (re)generated via top-up.

    Always the EMPTY partners (the model never voiced them). Plus — when
    the joint stream was cut by the token/context budget
    (``joint_truncated``, Ollama done_reason == "length") — the last
    non-empty partner in emission order, because that is the speaker the
    truncation caught mid-beat (the "second person cut off" regression).
    Partners emitted after the cut are already empty and thus already
    covered. Pure function; see tools/test_multi_topup.py.
    """
    targets = {n for n in partner_names if not (bodies.get(n) or "").strip()}
    if joint_truncated:
        for n in reversed(partner_names):
            if (bodies.get(n) or "").strip():
                targets.add(n)
                break
    return targets


def _dispatch_multi_response(
    cid: str,
    *,
    partners: list[str],
    lead_msg: dict[str, Any],
    raw_lead_output: str,
    lead_persona: str,
    joint_truncated: bool = False,
) -> list[dict[str, Any]]:
    """RETIRED (kept for rollback) — the old single-joint-call dispatcher.

    The route no longer calls this: multi-response is now the CHAIN
    architecture (`_stream_partner_chain`), where the lead is a normal turn
    and each partner is its own real single-character turn. This function
    (and `_generate_partner_topup`, `split_joint_n`, `JointStreamRouter`,
    the voice/directive blocks) are the joint-call machinery, retained one
    release for rollback and still covered by the legacy tests
    (test_multi_response_topup.py, test_multi_response_lead_cutoff.py).
    Delete once the chain path is confirmed in live play.

    Split the lead's streamed output (which contains the full
    multi-character scene because the directive told the model to
    voice everyone and we dropped every partner's `<Name>:` stop)
    into per-speaker bodies, trim the lead's persisted content to
    just its own portion, and persist each partner as a chained
    child of the previous speaker.

    Returns the list of partner messages persisted, in order. Empty
    list on parse failure (logs and continues).

    Persistence shape — linear chain so the branch UI reads as one
    trail of speakers rather than fanned-out alternatives:

        user
        └── lead
            └── partner1
                └── partner2
                    └── partner3   <- active leaf

    Single LLM call for the whole scene; this dispatcher only does
    the post-stream split + persistence. (The directive that makes
    the model voice everyone lives in app/multi_response.py and is
    appended to the lead's system prompt at route entry.)
    """
    live_conv = convs.load_conversation(cid)
    if not live_conv or not lead_msg:
        return []

    lead_name = mr.display_name_of(cid, lead_persona)
    partner_names = [mr.display_name_of(cid, p) for p in partners]
    # Build the canonical → [aliases] map so split_joint_n catches any
    # form the model used (e.g. `Priya:` for `Dr. Priya Anand:`).
    # Bodies are keyed by canonical so the per-partner lookup below
    # still works.
    labels_by_canonical: dict[str, list[str]] = {
        lead_name: mr.display_labels_of(cid, lead_persona),
    }
    for p, pn in zip(partners, partner_names):
        labels_by_canonical[pn] = mr.display_labels_of(cid, p)
    lead_snap = lead_msg.get("presence_snapshot") or {}
    out: list[dict[str, Any]] = []

    # Inline-label scrub: rescue dialogue the model wrote as
    # `*action.* "line." OtherName: "their line."` mid-block — without
    # this, OtherName's content stays glued to the previous speaker
    # because split_joint_n's boundary regex is line-anchored.
    normalized = mr.normalize_inline_labels(raw_lead_output, labels_by_canonical)
    bodies, _ = mr.split_joint_n(normalized, labels_by_canonical)
    lead_body = bodies.get(lead_name, "").strip()
    any_partner_spoke = any(bodies.get(n, "").strip() for n in partner_names)
    missing_partners = [n for n in partner_names if not bodies.get(n, "").strip()]
    if missing_partners:
        log.warning(
            "multi-response: partners produced no content cid=%s lead=%s missing=%s",
            cid, lead_persona, missing_partners,
        )

    # Which partners to (re)generate via top-up: the empties, plus the
    # speaker a budget-truncated joint cut off mid-beat — the "second
    # person cut off" symptom on big prompts (a large multi turn's system
    # prompt runs ~7.6k tokens, so num_ctx leaves little room to voice
    # everyone). Regenerating the victim as a standalone beat (smaller
    # prompt, its own budget) replaces the cut-off fragment with a full
    # one; empty partners already topped up, a non-empty-but-truncated one
    # did not, which is the regression this closes.
    topup_targets = _multi_topup_targets(partner_names, bodies, joint_truncated)
    if joint_truncated:
        log.info(
            "multi-response: joint truncated (length) — top-up targets=%s cid=%s",
            sorted(topup_targets), cid,
        )

    # Trim lead's persisted content to its portion only — but only if
    # at least one partner actually appeared. If no partner spoke (the
    # directive failed and we got a single-character response), leave
    # the lead's content alone since the full output is just theirs.
    live_lead = (live_conv.get("messages") or {}).get(lead_msg["id"])
    if lead_body and any_partner_spoke and live_lead:
        live_lead["content"] = lead_body

    # Lead (first-responder) repair. When the joint stream was cut by the
    # token/context budget (joint_truncated) AND no partner produced any
    # content, the truncation landed INSIDE the lead's own beat before
    # generation ever reached a partner label — the "cuts off the first
    # responder" symptom. This is common in large-scale scenes: the multi
    # prompt runs ~7k tokens against num_ctx=8192, so a long conversation
    # leaves almost no generation headroom and Ollama returns done="length"
    # mid-lead. The partner top-up below never fixes this (the lead is not
    # a top-up target), so regenerate the lead the same way partners are
    # rescued: a NORMAL single-character turn anchored at the lead's parent
    # (its own ~3k-token-smaller prompt has room to finish). done="stop"
    # with no partners is the directive simply failing on a COMPLETE lead
    # beat — left alone, only length truncation triggers repair.
    lead_repaired = False
    if joint_truncated and not any_partner_spoke and live_lead:
        lead_parent = live_lead.get("parent_id") or lead_msg.get("parent_id")
        new_lead = _generate_partner_topup(cid, live_conv, lead_parent, lead_persona)
        if new_lead:
            live_lead["content"] = new_lead
            lead_repaired = True
            log.info(
                "multi-response: lead truncated (length) — regenerated cid=%s lead=%s",
                cid, lead_persona,
            )
        else:
            log.warning(
                "multi-response: lead top-up produced nothing cid=%s lead=%s",
                cid, lead_persona,
            )

    # Stamp the lead with its multi_response group tag so client-side
    # regen-group can find every member by group_id.
    if live_lead:
        grp: dict[str, Any] = {
            "group_id": lead_msg["id"],
            "role": "lead",
            "ordinal": 0,
        }
        if lead_repaired:
            grp["topped_up"] = True
        live_lead.setdefault("metadata", {})["multi_response"] = grp

    # Chain each partner under the previous, in turn_order.
    prev_id = lead_msg["id"]
    for ordinal, (partner_id, partner_name) in enumerate(
        zip(partners, partner_names), start=1
    ):
        body = bodies.get(partner_name, "").strip()
        topped_up = False
        if partner_name in topup_targets:
            # Hybrid top-up: the joint output left this roster member
            # empty (model spiral / under-production) OR cut them off
            # mid-beat when the joint hit the token/context budget (see
            # _multi_topup_targets). Either way, regenerate their beat as a
            # normal single-character turn against the chain so far — real
            # prompt, full stop list, no split, and a smaller prompt than
            # the joint so it has room to finish — hardened with three
            # guards (repeat-stop, no-think, 512-token cap) so it can't
            # itself spiral. Costs one extra model call only on turns that
            # actually failed. See tools/test_multi_response_topup.py.
            new_body = _generate_partner_topup(cid, live_conv, prev_id, partner_id)
            if new_body:
                body = new_body
                topped_up = True
            elif not body:
                log.warning(
                    "multi-response: top-up produced nothing cid=%s partner=%s",
                    cid, partner_id,
                )
                continue
            # else: regen failed but we have the partial joint body — keep
            # it rather than dropping the speaker entirely.
        meta_grp: dict[str, Any] = {
            "group_id": lead_msg["id"],
            "role": "partner",
            "ordinal": ordinal,
        }
        if topped_up:
            meta_grp["topped_up"] = True
        partner_msg = convs.append_message(
            live_conv,
            parent_id=prev_id,
            persona=partner_id,
            content=body,
            speaker_id=partner_id,
            presence_snapshot=lead_snap,
            metadata={"multi_response": meta_grp},
        )
        run_message_annotators(live_conv, partner_msg)
        out.append(partner_msg)
        prev_id = partner_msg["id"]
    convs.save_conversation(live_conv)

    # Side-by-side composite: when 2+ group members are combined-format
    # characters, build one shared image and stamp it on every member,
    # overriding the per-character pick the inline picker may have set
    # on the lead. Falls back to per-character picks when fewer than 2
    # combined-format participants are present.
    try:
        if _stamp_group_image(cid, live_conv, lead_msg["id"]):
            convs.save_conversation(live_conv)
    except Exception:
        log.exception("group image stamp failed cid=%s", cid)

    return out


def _generate_partner_topup(
    cid: str,
    live_conv: dict[str, Any],
    parent_id: str,
    partner_id: str,
) -> str:
    """Hybrid completeness top-up for multi-response.

    When the joint single-call output left a roster member with an
    empty body, generate their beat as a single-character turn anchored
    at the chain persisted so far (``parent_id`` = previous speaker's
    message). Uses the standard prompt assembly — full stop list, no
    ``_multi`` extra, no split — so the failure modes of the joint call
    (label drift, presence spirals) don't apply. It is deliberately
    MORE conservative than a normal turn: ``think=False``,
    ``stop_on_repeat=True``, and a 512-token cap bound it to one beat
    that can't run away; the meta scrub + prefix strip clean the result.
    Returns the cleaned body, or "" on any failure (caller leaves the
    speaker silent, as before).
    """
    try:
        settings = live_conv.get("settings") or {}
        model = settings.get("ollama_model_override") or (
            current_app.config.get("ollama") or {}
        ).get("model")
        profile = (current_app.config.get("model_profiles") or {}).get(model) or {}
        sampling = {**profile, **(settings.get("sampling") or {})}
        prompt = assemble_prompt(
            live_conv, partner_id, speaker_id=partner_id, leaf_id=parent_id,
        )
        auto_stop = list(prompt.get("stop") or [])
        if auto_stop:
            sampling["stop"] = list({*(sampling.get("stop") or []), *auto_stop})
        if not sampling.get("num_predict"):
            sampling["num_predict"] = 512  # one beat, bounded
        parts: list[str] = []
        for ev in chat_stream(
            system=prompt["system"],
            messages=prompt["messages"],
            model=model,
            options=sampling,
            think=False,
            stop_on_repeat=True,
        ):
            if ev["kind"] != "thinking":
                parts.append(ev["text"])
        raw = "".join(parts)
        if raw.startswith("[ollama error:") or raw.startswith("[chat_stream raised:"):
            log.warning("multi top-up error cid=%s partner=%s: %s", cid, partner_id, raw[:160])
            return ""
        name = mr.display_name_of(cid, partner_id)
        body = _strip_speaker_prefix(raw, name).strip()
        body = run_output_filters(live_conv, body)
        return mr.strip_meta_commentary(body).strip()
    except Exception:
        log.exception("multi top-up crashed cid=%s partner=%s", cid, partner_id)
        return ""


def _stream_partner_chain(
    cid: str,
    *,
    lead_msg: dict[str, Any],
    partners: list[str],
    model: str | None,
    base_sampling: dict[str, Any],
    enable_thinking: bool = False,
):
    """Stream each partner as its OWN real single-character turn, chained
    under the lead — the chain multi-response architecture.

    This is a GENERATOR: it yields SSE event strings (per-partner ``delta``
    events tagged with the partner's ``speaker_id`` so the client paints
    them into the placeholder bubbles created by ``multi_placeholders``,
    then a ``multi_message`` when each partner persists) and RETURNS the
    last persisted partner message (captured by the route via
    ``yield from``), or ``None`` if nobody spoke.

    Each partner is a normal ``assemble_prompt`` turn anchored at the
    growing chain leaf, so it reacts to the inciting incident AND sees the
    lead and any earlier partners — and its prompt is single-character
    sized, so it fits ``num_ctx`` where the old joint prompt overflowed.
    Partners are hardened like the former top-up (``stop_on_repeat`` + a
    generous ``num_predict`` bound + meta scrub) so one can't stall the
    chain; thinking is captured to metadata but not streamed (it would
    land in the shared trace UI). Persistence mirrors the old dispatcher:
    linear chain, ``multi_response`` group tag, annotators, sprite pick,
    and a final group-image stamp.
    """
    live_conv = convs.load_conversation(cid)
    if not live_conv or not lead_msg:
        return None
    lead_id = lead_msg["id"]
    lead_snap = lead_msg.get("presence_snapshot") or {}

    # Stamp the lead as the group's lead so client-side regen-group can find
    # every member by group_id (lead id == group_id; partners chain under it).
    live_lead = (live_conv.get("messages") or {}).get(lead_id)
    if live_lead is not None:
        live_lead.setdefault("metadata", {})["multi_response"] = {
            "group_id": lead_id, "role": "lead", "ordinal": 0,
        }
        convs.save_conversation(live_conv)

    last_msg: dict[str, Any] | None = None
    prev_id = lead_id
    for ordinal, partner_id in enumerate(partners, start=1):
        name = mr.display_name_of(cid, partner_id)
        full_prefix = f"{name}: " if name else None
        buf: list[str] = []
        resolved = False
        parts: list[str] = []
        think_parts: list[str] = []
        try:
            prompt = assemble_prompt(
                live_conv, partner_id, speaker_id=partner_id, leaf_id=prev_id,
            )
            sampling = dict(base_sampling)
            auto_stop = list(prompt.get("stop") or [])
            if auto_stop:
                sampling["stop"] = list({*(sampling.get("stop") or []), *auto_stop})
            if not sampling.get("num_predict"):
                sampling["num_predict"] = 1024  # generous; a single beat is far under
            for ev in chat_stream(
                system=prompt["system"], messages=prompt["messages"],
                model=model, options=sampling, think=enable_thinking,
                stop_on_repeat=True,
            ):
                if ev["kind"] == "thinking":
                    think_parts.append(ev["text"])
                    continue
                parts.append(ev["text"])
                # Live per-partner delta, stripping a leading "<Name>: "
                # prefix the model may echo (mirrors the lead's handling).
                if not resolved and full_prefix:
                    buf.append(ev["text"])
                    combined = "".join(buf).lstrip()
                    if combined.startswith(full_prefix):
                        rest = combined[len(full_prefix):]
                        resolved = True
                        buf = []
                        if rest:
                            yield _sse_event({"type": "delta", "speaker_id": partner_id, "content": rest})
                    elif full_prefix.startswith(combined):
                        pass  # still could be the prefix; keep buffering
                    else:
                        resolved = True
                        yield _sse_event({"type": "delta", "speaker_id": partner_id, "content": "".join(buf)})
                        buf = []
                else:
                    yield _sse_event({"type": "delta", "speaker_id": partner_id, "content": ev["text"]})
        except Exception:
            log.exception("partner stream failed cid=%s partner=%s", cid, partner_id)
            continue
        if buf and not resolved:
            yield _sse_event({"type": "delta", "speaker_id": partner_id, "content": "".join(buf)})

        raw = "".join(parts)
        if raw.startswith("[ollama error:") or raw.startswith("[chat_stream raised:"):
            log.warning("partner stream error cid=%s partner=%s: %s", cid, partner_id, raw[:160])
            continue
        body = _strip_speaker_prefix(raw, name).strip()
        body = run_output_filters(live_conv, body)
        body = mr.strip_meta_commentary(body).strip()
        if not body:
            log.info("multi-response: partner produced no content cid=%s partner=%s", cid, partner_id)
            continue

        meta: dict[str, Any] = {
            "multi_response": {"group_id": lead_id, "role": "partner", "ordinal": ordinal},
        }
        think_text = "".join(think_parts).strip()
        if think_text:
            meta["thinking"] = think_text
        try:
            msg = convs.append_message(
                live_conv, parent_id=prev_id, persona=partner_id, content=body,
                speaker_id=partner_id, presence_snapshot=lead_snap, metadata=meta,
            )
            run_message_annotators(live_conv, msg)
            pick = _inline_sprite_pick(cid, live_conv, msg, partner_id)
            if pick:
                msg.setdefault("metadata", {})["image_pack_pick"] = pick
            convs.save_conversation(live_conv)
        except Exception:
            log.exception("partner persist failed cid=%s partner=%s", cid, partner_id)
            continue

        yield _sse_event({"type": "multi_message", "message": msg})
        last_msg = msg
        prev_id = msg["id"]

    # Side-by-side composite when 2+ members are combined-format characters
    # (mirrors the old dispatcher); no-op otherwise.
    try:
        if _stamp_group_image(cid, live_conv, lead_id):
            convs.save_conversation(live_conv)
    except Exception:
        log.exception("group image stamp failed cid=%s", cid)

    return last_msg


def _resolve_auto_next_responder(
    conv: dict[str, Any],
    finished_msg: dict[str, Any],
    finished_persona: str,
) -> str | None:
    """`turn_mode=auto` resolver. Pick who should speak next from the
    just-completed turn (or multi-response chain), using a strict
    priority order so the behaviour is predictable:

      1. Latest `[next: <id>]` directive in the chain that names a
         character on the effective cast and didn't just speak.
      2. Latest name-mention in any chain message body that resolves
         to an in-cast character who didn't just speak.
      3. Round-robin advance through `turn_order` from the finished
         speaker.

    Returns the picked character id, or None to leave the client's
    Reply-as picker untouched (in which case autoplay fires whatever
    is currently in the dropdown — typically the same speaker).

    The user's force-override is the Reply-as dropdown itself: this
    function returns a *suggestion* the client parks there. The user
    can change it before the autoplay countdown fires.

    Composes with `multi_response`: we walk the multi-response group
    chain (lead + every partner) so a `[next: ...]` emitted by the
    lead is honored even when the last persisted message in the
    chain is a partner.  The picked next-responder becomes the
    lead of the *next* multi-response if multi is enabled.
    """
    cid = conv.get("id") or ""
    leaf_id = conv.get("active_path_leaf") or finished_msg.get("id") or ""

    # Build the just-spoke set: every character on the chain we're
    # exiting. If multi_response wrapped this turn, that's everyone
    # in the group; otherwise it's just `finished_persona`.
    just_spoke: set[str] = {finished_persona}
    chain_msgs: list[dict[str, Any]] = [finished_msg]
    fmeta = finished_msg.get("metadata") or {}
    fgroup = (fmeta.get("multi_response") or {}).get("group_id")
    if fgroup:
        # Walk the path; collect any message tagged with the same
        # group_id. Lead's id == group_id; partners chain under it.
        msgs_map = conv.get("messages") or {}
        for m in msgs_map.values():
            mmeta = (m.get("metadata") or {}).get("multi_response") or {}
            if mmeta.get("group_id") == fgroup:
                if m.get("speaker_id"):
                    just_spoke.add(m["speaker_id"])
                if m["id"] != finished_msg["id"]:
                    chain_msgs.append(m)

    # Sort chain newest-first so we honor the latest directive when
    # multiple were emitted across partners.
    chain_msgs.sort(key=lambda m: m.get("created_at", 0), reverse=True)

    # Resolve in-cast set so we can validate any candidate.
    try:
        cast = effective_entities_at(conv, leaf_id) or {}
    except Exception:
        cast = {}
    in_cast = {
        eid for eid, e in cast.items()
        if isinstance(e, dict) and e.get("type") == "character"
    }

    # Step 1: scan applied_edits for [next: <id>] directives,
    # newest-first; first valid wins.
    for m in chain_msgs:
        applied = ((m.get("metadata") or {}).get("applied_edits") or [])
        for entry in reversed(applied):
            if entry.get("kind") != "next":
                continue
            candidate = (entry.get("id") or "").strip().lower()
            if not candidate:
                continue
            # `user` is a valid handoff target: returning "user" tells
            # the client "stop auto-firing, wait for the user."  Stream
            # callers treat None and "user" the same — the dropdown
            # update is suppressed so the user can take the next turn.
            if candidate == "user":
                return "user"
            if candidate in just_spoke:
                # Same speaker just talked — ignore self-handoffs.
                continue
            if candidate in in_cast:
                return candidate

    # Step 2: name-mention scan, newest-first, longest-name-first.
    candidates = sorted(
        (
            (eid, (e.get("name") or "").strip())
            for eid, e in cast.items()
            if isinstance(e, dict) and e.get("type") == "character"
            and eid not in just_spoke
        ),
        key=lambda t: -len(t[1]),
    )
    for m in chain_msgs:
        body = (m.get("content") or "")
        if not body:
            continue
        for eid, name in candidates:
            if not name:
                continue
            # Word-boundary match on either full name or first token,
            # mirroring the client-side pickResponderFromMention.
            patt = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
            if patt.search(body):
                return eid
            first = name.split()[0] if name.split() else ""
            if first and len(first) >= 3:
                pattF = re.compile(rf"\b{re.escape(first)}\b", re.IGNORECASE)
                if pattF.search(body):
                    return eid

    # Step 3: round-robin through turn_order, advancing from the
    # finished speaker.
    order = list((conv.get("settings") or {}).get("turn_order") or [])
    if order and finished_persona in order:
        for step in range(1, len(order) + 1):
            cand = order[(order.index(finished_persona) + step) % len(order)]
            if cand and cand != "user" and cand not in just_spoke:
                return cand
            if cand == finished_persona:
                break
    return None


def _stamp_group_image(
    cid: str, live_conv: dict[str, Any], group_id: str
) -> bool:
    """Walk the active path, collect every member of ``group_id`` in
    ordinal order, and stamp ``metadata.image_pack_pick`` with the
    side-by-side composite produced by ``_maybe_group_sprite_pick``.

    Returns True when a group image was produced and stamped, False
    otherwise (no/insufficient combined-format participants, etc.).
    The caller is responsible for ``save_conversation`` after a True
    return; this function only mutates in-memory metadata.

    GATED on the conversation's ``image_pack_pick`` setting (same
    three-state precedence: per-conv true → on, per-conv false → off,
    per-conv null → fall back to ``config.defaults.image_pack_pick``,
    default false). Without this gate the multi-response flow would
    stamp group composites even when the user has image mode off —
    effectively turning image mode on for that group. Mirrors the
    `_inline_sprite_pick` self-gate in this same file.
    """
    settings = live_conv.get("settings") or {}
    enabled = settings.get("image_pack_pick")
    if enabled is False:
        return False
    if enabled is None:
        defaults = current_app.config.get("defaults") or {}
        if not bool(defaults.get("image_pack_pick", False)):
            return False

    msgs = live_conv.get("messages") or {}
    leaf = live_conv.get("active_path_leaf") or ""
    chain = convs.path_to_root(live_conv, leaf) if leaf else []
    members = [
        m for m in chain
        if ((m.get("metadata") or {}).get("multi_response") or {}).get("group_id") == group_id
    ]
    members.sort(key=lambda m: ((m.get("metadata") or {}).get("multi_response") or {}).get("ordinal", 0))
    if len(members) < 2:
        return False
    from .api import _maybe_group_sprite_pick
    pick = _maybe_group_sprite_pick(cid, live_conv, members)
    if not pick:
        return False
    for m in members:
        m.setdefault("metadata", {})["image_pack_pick"] = pick
    return True


def _inline_sprite_pick(
    cid: str, conv: dict[str, Any], msg: dict[str, Any], speaker_id: str
) -> dict[str, Any] | None:
    """Run the deterministic sprite pick for a combined-format speaker.

    Reads ``effective_entities_at`` at the message itself so any edits
    extracted from the message body (narrator directives like
    `[outfit ...]` / `[set ...clothing_overrides...]`) are applied
    BEFORE the image is composed — otherwise prose that says "she
    peels off her bra" along with `[set bra=3]` would still render
    her bra-on. Returns None when the speaker doesn't carry a sprite
    or any required field is missing — in that case the legacy POST
    /image_pick path stays in charge (e.g. a tagged catalog character).
    """
    if not (conv and msg and speaker_id):
        return None
    settings = conv.get("settings") or {}
    enabled = settings.get("image_pack_pick")
    if enabled is False:
        return None
    if enabled is None:
        defaults = current_app.config.get("defaults") or {}
        if not bool(defaults.get("image_pack_pick", False)):
            return None
    try:
        from .api import _maybe_sprite_pick
        leaf_id = msg.get("id") or msg.get("parent_id") or ""
        eff = effective_entities_at(conv, leaf_id) if leaf_id else {}
        return _maybe_sprite_pick(cid, conv, msg, speaker_id, eff=eff)
    except Exception:
        log.exception("inline sprite pick failed cid=%s mid=%s", cid, msg.get("id"))
        return None


def _focal_speaker_name(cid: str, persona: str, speaker_id: str | None) -> str | None:
    """Return the display name to strip as a leading '<name>: ' echo from
    the model's output. The model sees every primer turn and history turn
    labelled as '<character_name>: <content>' and sometimes echoes the
    label back; we strip it on the way out."""
    if persona == "narrator":
        return "Narrator"
    speaker = speaker_id or persona
    if not speaker:
        return None
    try:
        entities = load_instance_entities(cid)
        ent = entities.get(speaker) or {}
        return ent.get("name") or speaker
    except Exception:
        return speaker


def _strip_speaker_prefix(text: str, name: str | None) -> str:
    """Strip a leading '<name>: ' (case-sensitive) from text, ignoring
    leading whitespace. Strips at most one occurrence at the start."""
    if not text or not name:
        return text
    import re
    return re.sub(r'^\s*' + re.escape(name) + r':\s*', '', text, count=1)


def _summarize_in_background(app, cid: str, dropped_messages: list[dict[str, str]],
                             focal_id: str | None = None) -> None:
    """Fire-and-forget: ask the model to summarize messages that were elided
    by truncation, then append the summary as a NEW FRAGMENT.

    PER-CHARACTER: when `focal_id` is a character, the fragment is stored under
    `settings.summary_fragments_by_focal[focal]` — the `dropped_messages` are
    already gated to that character's prompt, so their recap only covers what
    THEY witnessed. The narrator (focal None) writes the global
    `settings.summary_fragments` (authorial, omniscient). This closes the
    locational leak where every character read one omniscient summary.

    Fragment schema:
        {"text": str, "anchor_ids": list[str], "created_at": int}

    Older code wrote a single settings.summary string + a flat
    settings.summary_anchor_ids list, which leaked across branches
    because the staleness check used any() over a global anchor pool.
    We keep reading the legacy keys for back-compat (treated as one
    big fragment) but new writes only produce fragments.
    """
    if not dropped_messages:
        return
    lock = _summary_locks.setdefault(cid, threading.Lock())
    if not lock.acquire(blocking=False):
        return  # another summarizer is already running for this convo
    def run():
        try:
            with app.app_context():
                conv = convs.load_conversation(cid)
                if not conv:
                    return
                joined = "\n\n".join(
                    f"{m['role'].upper()}: {m['content']}"
                    for m in dropped_messages if m.get("content")
                ).strip()
                if not joined:
                    return
                # Anchor ids for this fragment specifically. Strict
                # per-branch staleness: the prompt assembler only
                # includes this fragment if EVERY anchor is on the
                # active path.
                anchor_ids = [m.get("__msg_id") for m in dropped_messages if m.get("__msg_id")]
                system = (
                    "You are a chat summarizer. Read the messages and output a "
                    "single 2-4 sentence past-tense summary of what happened, "
                    "who said what, and any state changes. No quotes around the "
                    "summary. No commentary. Just the summary text."
                )
                user = f"Summarize:\n\n{joined}"
                log.info("summarize cid=%s dropped=%d", cid, len(dropped_messages))
                # Pin to the conversation's main model + num_ctx so this
                # background call doesn't make Ollama unload/reload the
                # weights with a different engine config. Just matching
                # the model id isn't enough — Ollama treats a different
                # num_ctx as a different load and re-fetches the model.
                conv_settings = conv.get("settings") or {}
                model = conv_settings.get("ollama_model_override") or (
                    current_app.config.get("ollama") or {}
                ).get("model")
                summary_options: dict[str, Any] = {
                    "temperature": 0.3, "num_predict": 256,
                }
                sampling = conv_settings.get("sampling") or {}
                if isinstance(sampling, dict):
                    for k in ("num_ctx", "num_gpu", "num_thread"):
                        v = sampling.get(k)
                        if v not in (None, "", 0):
                            summary_options[k] = v
                summary = chat_sync(
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    model=model,
                    options=summary_options,
                    think=False,  # summarization, never reason
                )
                if not summary or summary.startswith("[ollama error"):
                    log.info("summarize cid=%s skipped (%s)", cid, summary[:60] if summary else "empty")
                    return
                fresh = convs.load_conversation(cid) or conv
                settings = fresh.setdefault("settings", {})
                # Append the new fragment; cap to FRAGMENT_CAP so the
                # summary block can't grow unbounded. 8 fragments × ~200
                # token summaries ≈ 1600 tokens max for the summary
                # block — leaves the bulk of context budget for history.
                FRAGMENT_CAP = 8
                frag = {
                    "text": summary.strip(),
                    "anchor_ids": [aid for aid in anchor_ids if aid],
                    "created_at": int(__import__("time").time()),
                }
                is_char = bool(focal_id) and focal_id != "narrator"
                if is_char:
                    # per-character store — this character's own witnessed recap
                    by_focal = settings.get("summary_fragments_by_focal")
                    if not isinstance(by_focal, dict):
                        by_focal = {}
                    lst = by_focal.get(focal_id)
                    if not isinstance(lst, list):
                        lst = []
                    lst.append(frag)
                    by_focal[focal_id] = lst[-FRAGMENT_CAP:]
                    settings["summary_fragments_by_focal"] = by_focal
                else:
                    # narrator / authorial — the global omniscient summary
                    fragments = settings.get("summary_fragments")
                    if not isinstance(fragments, list):
                        fragments = []
                    fragments.append(frag)
                    settings["summary_fragments"] = fragments[-FRAGMENT_CAP:]
                    # Wipe the legacy single-summary fields so old per-
                    # conversation pollution can't bleed in via that path.
                    settings.pop("summary", None)
                    settings.pop("summary_anchor_ids", None)
                convs.save_conversation(fresh)
                log.info("summarize cid=%s fragment_count=%d new_chars=%d new_anchors=%d",
                         cid, len(fragments), len(summary), len(anchor_ids))
        except Exception:
            log.exception("summarize failed cid=%s", cid)
        finally:
            lock.release()
    threading.Thread(target=run, daemon=True, name=f"summarize-{cid}").start()


def _extract_memory_in_background(app, cid: str, focal_id: str,
                                  dropped_messages: list[dict[str, str]]) -> None:
    """Fire-and-forget sibling of the summarizer: when turns a character
    WITNESSED age out of their window, extract the durable facts that character
    would retain and store them as branch-local `metadata.memory[focal]` (via
    memory.remember), so a salient detail survives truncation instead of being
    lost. Per-character (the dropped_messages are already gated to `focal_id`'s
    prompt) and best-effort. See docs/character_memory_design.md."""
    from .. import memory as _memory
    if not dropped_messages or not focal_id or focal_id == "narrator":
        return
    key = f"{cid}:{focal_id}"
    lock = _memory_locks.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        return

    def run():
        try:
            with app.app_context():
                conv = convs.load_conversation(cid)
                if not conv:
                    return
                joined = "\n\n".join(
                    f"{m['role'].upper()}: {m['content']}"
                    for m in dropped_messages if m.get("content")
                ).strip()
                if not joined:
                    return
                name = (conv.get("entities", {}).get(focal_id) or {}).get("name") or focal_id
                conv_settings = conv.get("settings") or {}
                model = conv_settings.get("ollama_model_override") or (
                    current_app.config.get("ollama") or {}
                ).get("model")
                options: dict[str, Any] = {"temperature": 0.2, "num_predict": 200}
                sampling = conv_settings.get("sampling") or {}
                if isinstance(sampling, dict):
                    for k in ("num_ctx", "num_gpu", "num_thread"):
                        v = sampling.get(k)
                        if v not in (None, "", 0):
                            options[k] = v

                def _chat(system, messages, opts):
                    return chat_sync(system=system, messages=messages, model=model,
                                     options=opts, think=False)

                # Consolidation-aware: give the extractor what this character
                # already knows so it skips duplicates and flags updates.
                from ..effective import memory_for_path as _mem_read
                existing = [r.get("text") for r in _mem_read(conv, focal_id) if r.get("text")]
                facts = _memory.extract_facts(joined, name, _chat,
                                              existing_facts=existing, options=options)
                if not facts:
                    return
                # Anchor on the newest dropped turn's own message id (an ancestor
                # of the active leaf, so on-path) — ties the memory to roughly
                # where it was learned, and a rewind past it un-learns it.
                anchor = next((m.get("__msg_id") for m in reversed(dropped_messages)
                               if m.get("__msg_id")), None)
                fresh = convs.load_conversation(cid) or conv
                if anchor and anchor not in (fresh.get("messages") or {}):
                    anchor = None  # branch changed under us; fall back to active leaf
                leaf = anchor or fresh.get("active_path_leaf")
                added = 0
                for fact in facts:
                    if _memory.remember(fresh, focal_id, fact["text"], source="witnessed",
                                        supersedes=fact.get("supersedes"), leaf_id=leaf):
                        added += 1
                if added:
                    convs.save_conversation(fresh)
                    log.info("memory cid=%s focal=%s captured=%d", cid, focal_id, added)
        except Exception:
            log.exception("memory extract failed cid=%s focal=%s", cid, focal_id)
        finally:
            lock.release()

    threading.Thread(target=run, daemon=True, name=f"memory-{cid}-{focal_id}").start()


bp = Blueprint("stream", __name__)
log = logging.getLogger("gemmasim.stream")


def _sse_event(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@bp.post("/conversations/<cid>/generate")
@login_required
def generate(cid: str):
    """Kick off a streamed generation.

    Body: {"persona": "narrator"|"<character_id>", "speaker_id": optional,
           "parent_id": optional (defaults to active leaf)}
    """
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json(silent=True) or {}
    persona = payload.get("persona") or "narrator"
    speaker_id = payload.get("speaker_id") or (
        persona if persona not in ("narrator", "user") else None
    )
    parent_id = payload.get("parent_id") or conv["active_path_leaf"]
    # `disable_multi`: per-call override so "regen this character" on a
    # multi-response message can re-roll just one node without dragging
    # the whole group along.
    disable_multi = bool(payload.get("disable_multi"))
    # `carry_chain_from`: when single-regenning a multi-response group
    # member, the original message id whose downstream chain should be
    # cloned under the new sibling so partners after the regenerated
    # node aren't orphaned. Only honored together with disable_multi.
    carry_chain_from = payload.get("carry_chain_from") if disable_multi else None
    # `carry_cast_from`: the id of the message being regenerated. Regen re-parents
    # to that message's PARENT and generates a fresh sibling — which drops the old
    # turn's applied_edits, so a cast add/remove made on it (a removed character)
    # would silently return. Carry those declarative cast edits onto the new sibling.
    carry_cast_from = payload.get("carry_cast_from")

    # Multi-response partner resolution happens BEFORE prompt assembly
    # now: the multi-character voice + directive blocks live in the
    # prompt registry (app/multi_response.py) and fire off
    # `extra_context["_multi"]`. We still resolve partners here because
    # the stop-list manipulation below needs the partner name set too.
    multi_partners: list[str] = []
    multi_partner_names: list[str] = []
    multi_lead_name: str | None = None
    multi_enabled = (
        mr.is_enabled(conv)
        and persona not in ("narrator", "user")
        and not disable_multi
    )
    if multi_enabled:
        multi_partners = mr.partners_for_lead(conv, persona, leaf_id=parent_id)
        if multi_partners:
            multi_lead_name = mr.display_name_of(cid, persona)
            multi_partner_names = [mr.display_name_of(cid, p) for p in multi_partners]
    # Chain architecture: the lead is a NORMAL turn. It reacts to the
    # inciting incident on its own (it already sees `[Others present]`),
    # then each partner responds as its own real single-character turn,
    # chained under the previous speaker (see `_stream_partner_chain`).
    # So we do NOT inject the `_multi` joint directive / partner voice
    # blocks into the lead's prompt anymore — that was the single-joint-call
    # design, which stuffed every partner into one ~10k-token prompt and
    # overflowed num_ctx at depth (the "cuts off the first responder" bug).
    multi_extra = None

    try:
        # parent_id is the leaf for history + effective-state replay. On
        # regen the on-disk active_path_leaf still points at the message
        # being replaced (the client cancels the debounced active-leaf
        # POST in streamGenerate), so trusting it here would leak the
        # original reply into the new prompt.
        prompt = assemble_prompt(
            conv, persona, speaker_id=speaker_id, leaf_id=parent_id,
            extra_context=multi_extra,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    settings = conv.get("settings", {}) or {}
    model = settings.get("ollama_model_override") or (
        current_app.config.get("ollama") or {}
    ).get("model")

    # Sampling precedence: per-conversation overrides win, then the per-model
    # profile, then ollama.default_options (handled inside chat_stream).
    profile = (current_app.config.get("model_profiles") or {}).get(model) or {}
    conv_sampling = settings.get("sampling") or {}
    sampling = {**profile, **conv_sampling}

    # Auto-derived stop strings from prompt assembly: prevents the model
    # from continuing past its own turn into another speaker.
    auto_stop = list(prompt.get("stop") or [])
    if auto_stop:
        existing = list(sampling.get("stop") or [])
        sampling["stop"] = list({*existing, *auto_stop})

    # Chain architecture: the lead is a normal turn, so it uses the normal
    # auto-stop list (merged above) and no special num_predict cap. Each
    # partner then generates as its own real single-character turn in
    # `_stream_partner_chain` after the lead persists — its own prompt,
    # its own stops, chained under the previous speaker. No joint call, so
    # no stop surgery and no roster-sized generation cap here.
    log.info(
        "multi-response: setting=%s persona=%s partners=%s",
        bool((conv.get("settings") or {}).get("multi_response")),
        persona,
        multi_partners or "[]",
    )

    sys_chars = len(prompt["system"])
    hist_chars = sum(len(m["content"]) for m in prompt["messages"])
    enable_thinking = bool(settings.get("enable_thinking", False))
    log.info(
        "generate cid=%s persona=%s speaker=%s model=%s msgs=%d sys=%dch hist=%dch think=%s sampling=%s",
        cid, persona, speaker_id, model, len(prompt["messages"]),
        sys_chars, hist_chars, enable_thinking, sampling or "default",
    )
    if log.isEnabledFor(logging.DEBUG):
        log.debug("--- system prompt ---\n%s", prompt["system"])
        for m in prompt["messages"]:
            log.debug("--- %s ---\n%s", m["role"], m["content"])

    def gen() -> Iterator[str]:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        first_chunk_at: float | None = None
        started = time.monotonic()
        persisted = {"done": False}

        # Speaker-prefix stripping: every primer turn the model saw was
        # labelled "<name>: <content>", so it sometimes echoes the prefix
        # back. We buffer the first content chunks until we know whether
        # the prefix is there, then strip it both from what we yield to
        # the client and from what we persist.
        focal_name = _focal_speaker_name(cid, persona, speaker_id)
        full_prefix = f"{focal_name}: " if focal_name else None
        prefix_resolved = {"done": False}
        prefix_buffer: list[str] = []

        # Chain architecture: the lead streams into its own bubble like any
        # normal turn (no joint split/router). Partners stream afterward,
        # one real turn each, in `_stream_partner_chain`.

        def persist_partial(reason: str) -> dict | None:
            """Save whatever we've accumulated as a normal message in the
            tree. Idempotent — only the first call writes."""
            if persisted["done"]:
                return None
            persisted["done"] = True
            full_text = "".join(content_parts)
            # Strip a leading "<name>: " label from the model's output —
            # see _strip_speaker_prefix for why.
            full_text = _strip_speaker_prefix(full_text, focal_name)
            live_conv = convs.load_conversation(cid) or conv
            # Module output filters run on the generated text before it's
            # persisted — e.g. pf1e scrubs a leaked [Roll] block. No-op when
            # no module registered a filter (or none is active on the branch).
            full_text = run_output_filters(live_conv, full_text)
            thinking_text = "".join(thinking_parts)
            if not full_text.strip() and not thinking_text.strip():
                return None
            prose, edits = (
                extract_edits(full_text) if persona == "narrator" else (full_text, [])
            )
            try:
                # Apply narrator edits to the instance now, capturing a
                # presence patch + before-images for revert. Edits are
                # gated by a per-conversation toggle so the user can
                # switch back to manual review if they want.
                live_settings = live_conv.get("settings") or {}
                auto_apply = bool(live_settings.get("auto_apply_narrator_edits", True))
                presence_patch: dict[str, Any] = {}
                applied_log: list[dict[str, Any]] = []
                if edits and auto_apply:
                    parent_msg = live_conv["messages"].get(parent_id) or {}
                    parent_snap = parent_msg.get("presence_snapshot") or {}
                    user_persona = live_settings.setdefault("user_persona", {"name": "User", "description": ""})
                    from ..effective import effective_cast_at as _ec
                    existing_cast = _ec(live_conv, parent_id).get("characters") or set()
                    presence_patch, applied_log = apply_edits(
                        cid, edits, parent_snap, user_persona=user_persona,
                        existing_cast_chars=existing_cast,
                    )

                # Build the new message's presence_snapshot from the parent's
                # snapshot + any move/outfit patches the narrator just made.
                new_snap = None
                if presence_patch:
                    parent_msg = live_conv["messages"].get(parent_id) or {}
                    parent_snap = parent_msg.get("presence_snapshot") or {}
                    presence = dict(parent_snap.get("presence") or {})
                    for char_id, patch in presence_patch.items():
                        prev = dict(presence.get(char_id) or {})
                        prev.update({k: v for k, v in patch.items() if v})
                        presence[char_id] = prev
                    new_snap = {**parent_snap, "presence": presence}

                # Carry declarative cast edits (add/remove) from the regenerated
                # turn so a removed/added character stays removed/added across a
                # regen (see carry_cast_from). Idempotent set-membership ops, so
                # prepending them to this sibling's log is safe.
                if carry_cast_from:
                    src = live_conv["messages"].get(carry_cast_from) or {}
                    src_log = (src.get("metadata") or {}).get("applied_edits") or []
                    carried = [dict(e) for e in src_log if isinstance(e, dict)
                               and e.get("kind") in ("cast_add", "cast_remove") and e.get("id")]
                    if carried:
                        applied_log = carried + applied_log

                meta: dict[str, Any] = {}
                if applied_log:
                    meta["applied_edits"] = applied_log
                if edits and not auto_apply:
                    meta["pending_edits"] = edits
                if thinking_text:
                    meta["thinking"] = thinking_text
                phrase_hits = banned_phrase_hits(prose)
                if phrase_hits:
                    meta["phrase_hits"] = phrase_hits
                if reason != "complete":
                    meta["interrupted"] = reason
                msg = convs.append_message(
                    live_conv,
                    parent_id=parent_id,
                    persona=persona,
                    content=prose,
                    speaker_id=speaker_id,
                    presence_snapshot=new_snap,
                    metadata=meta or None,
                )
                # Let modules stamp branch-specific facts onto the node
                # (e.g. texting marks a reply as an SMS so it re-renders
                # on reload) — same node-metadata model as applied_edits.
                run_message_annotators(live_conv, msg)
                # Group rotation runs only for clean completions.
                next_responder = None
                if reason == "complete":
                    live_settings = live_conv.setdefault("settings", {})
                    mode = live_settings.get("turn_mode")
                    if mode == "rotating":
                        order = list(live_settings.get("turn_order") or [])
                        if order and persona in order:
                            idx = (order.index(persona) + 1) % len(order)
                            live_settings["turn_index"] = idx
                            next_responder = order[idx]
                    elif mode == "auto":
                        # `auto` mode: pick the next responder from
                        # (1) the just-completed chain's most recent
                        # `[next: <id>]` directive that names someone
                        # who didn't already speak in this chain,
                        # falling back to (2) a name-mention scan of
                        # the chain bodies, falling back to (3)
                        # round-robin through turn_order. The user
                        # always wins via the Reply-as dropdown — the
                        # value we set here is a suggestion the client
                        # parks in the dropdown; the user can change
                        # it before autoplay fires.
                        next_responder = _resolve_auto_next_responder(
                            live_conv, msg, persona,
                        )

                # Inline deterministic sprite pick for combined-format
                # characters. Skips the browser's separate POST round-trip
                # to /image_pick — same effective-state walk, but on the
                # server-side we already have the conversation loaded.
                # Tagged-format (tagged catalog pick) requires a model
                # call; that stays on the separate endpoint so streaming
                # finalisation isn't blocked on it. The optional
                # auto-state-changes side call is also model-driven and
                # lives on its own POST endpoint (/messages/<mid>/auto_state)
                # — the browser fires it after the stream closes so this
                # response doesn't stall waiting for a second round trip.
                if reason == "complete" and persona not in ("narrator", "user") and speaker_id:
                    pick = _inline_sprite_pick(cid, live_conv, msg, speaker_id)
                    if pick:
                        msg.setdefault("metadata", {})["image_pack_pick"] = pick
                convs.save_conversation(live_conv)
                log.info(
                    "generate %s cid=%s elapsed=%.2fs chars=%d edits=%d dropped=%d",
                    reason, cid, time.monotonic() - started, len(full_text), len(edits),
                    len(prompt.get("dropped_messages") or []),
                )
                return {
                    "message": msg,
                    "edits": edits,
                    "applied_edits": applied_log,
                    "active_path_leaf": live_conv["active_path_leaf"],
                    "next_responder": next_responder,
                }
            except Exception:
                log.exception("persist_partial failed cid=%s", cid)
                return None

        # Emit a 2KB padding comment first so any proxy / buffer flushes early
        # and the browser begins receiving data right away.
        yield ":" + " " * 2048 + "\n\n"
        yield _sse_event({"type": "start", "persona": persona, "speaker_id": speaker_id, "model": model})
        # Multi-response: announce the partner roster up front so the
        # client can build empty bubbles for each one before any tokens
        # arrive. The lead's bubble is the existing stream placeholder;
        # partners fill via `_stream_partner_chain` after the lead finishes.
        if multi_partners:
            yield _sse_event({
                "type": "multi_placeholders",
                "lead": {"speaker_id": speaker_id or persona, "name": multi_lead_name},
                "partners": [
                    {"speaker_id": pid, "name": pname}
                    for pid, pname in zip(multi_partners, multi_partner_names)
                ],
            })
        try:
            try:
                for ev in chat_stream(
                    system=prompt["system"],
                    messages=prompt["messages"],
                    model=model,
                    options=sampling,
                    think=enable_thinking,
                ):
                    if first_chunk_at is None:
                        first_chunk_at = time.monotonic()
                        log.info("first chunk after %.2fs (kind=%s)", first_chunk_at - started, ev["kind"])
                    if ev["kind"] == "thinking":
                        thinking_parts.append(ev["text"])
                        yield _sse_event({"type": "thinking", "content": ev["text"]})
                    else:
                        content_parts.append(ev["text"])
                        if not prefix_resolved["done"] and full_prefix:
                            prefix_buffer.append(ev["text"])
                            combined = "".join(prefix_buffer).lstrip()
                            if combined.startswith(full_prefix):
                                # Prefix matched — strip it and yield the rest.
                                rest = combined[len(full_prefix):]
                                prefix_resolved["done"] = True
                                prefix_buffer.clear()
                                if rest:
                                    yield _sse_event({"type": "delta", "content": rest})
                            elif full_prefix.startswith(combined):
                                # Could still be the prefix; keep buffering.
                                pass
                            else:
                                # Definitely not the prefix; flush buffered as-is.
                                prefix_resolved["done"] = True
                                yield _sse_event({"type": "delta", "content": "".join(prefix_buffer)})
                                prefix_buffer.clear()
                        else:
                            if not prefix_resolved["done"]:
                                prefix_resolved["done"] = True  # no focal name to strip
                            yield _sse_event({"type": "delta", "content": ev["text"]})
            except Exception as e:  # network failure, ollama down, etc.
                log.exception("stream failed")
                # Persist what we have anyway, so it isn't lost.
                persist_partial("error")
                yield _sse_event({"type": "error", "error": str(e)})
                return

            # Flush any remaining prefix-buffer that never resolved (very
            # short reply that ended mid-prefix-check).
            if prefix_buffer and not prefix_resolved["done"]:
                yield _sse_event({"type": "delta", "content": "".join(prefix_buffer)})
                prefix_buffer.clear()
                prefix_resolved["done"] = True

            result = persist_partial("complete") or {}

            # Multi-response (chain): after the lead persists, stream each
            # partner as its OWN real single-character turn, chained under
            # the previous speaker. `_stream_partner_chain` yields the SSE
            # events (per-partner deltas + a `multi_message` when each one
            # persists) and returns the last persisted partner (or None).
            # Each partner reacts to the inciting incident AND sees the lead
            # and earlier partners (it's assembled at the growing leaf), and
            # each prompt is single-character sized so it fits num_ctx.
            last_partner: dict[str, Any] | None = None
            if multi_partners and result.get("message"):
                try:
                    last_partner = yield from _stream_partner_chain(
                        cid,
                        lead_msg=result["message"],
                        partners=multi_partners,
                        model=model,
                        base_sampling={**profile, **conv_sampling},
                        enable_thinking=enable_thinking,
                    )
                except Exception:
                    log.exception("partner chain failed cid=%s", cid)
            elif carry_chain_from and result.get("message"):
                # Single-regen of a multi-response group member: the new
                # sibling stands alone unless we carry the original's
                # downstream chain across as clones, so the partners
                # below the regenerated node aren't orphaned on the old
                # branch.
                try:
                    for partner_msg in _carry_group_chain(
                        cid, original_id=carry_chain_from, new_msg=result["message"],
                    ):
                        yield _sse_event({"type": "multi_message", "message": partner_msg})
                        last_partner = partner_msg
                except Exception:
                    log.exception("multi-response carry failed cid=%s", cid)

            # Auto-summarize whatever truncation dropped.
            dropped_msgs = prompt.get("dropped_messages") or []
            if dropped_msgs:
                _summarize_in_background(
                    current_app._get_current_object(), cid, list(dropped_msgs), persona
                )
                # Auto-capture: harvest the durable facts the responding
                # character witnessed in the turns that just aged out of their
                # window, before they're lost. (persona is the focal character
                # here; the dropped set is already gated to their prompt.)
                if persona and persona != "narrator":
                    _extract_memory_in_background(
                        current_app._get_current_object(), cid, persona, list(dropped_msgs)
                    )

            # If partners were added, the active leaf advanced onto the last
            # partner (each append_message advances it). Reload for the done
            # event's leaf, and re-resolve next_responder against the last
            # partner so a `[next: ...]` on any chain segment is honored and
            # the just-spoke set includes every partner.
            if last_partner:
                fresh = convs.load_conversation(cid)
                if fresh:
                    result["active_path_leaf"] = fresh.get("active_path_leaf")
                    if (fresh.get("settings") or {}).get("turn_mode") == "auto":
                        lp_persona = (
                            last_partner.get("speaker_id")
                            or last_partner.get("persona")
                            or ""
                        )
                        try:
                            result["next_responder"] = _resolve_auto_next_responder(
                                fresh, last_partner, lp_persona,
                            )
                        except Exception:
                            log.exception("auto next_responder re-resolve failed cid=%s", cid)

            yield _sse_event(
                {
                    "type": "done",
                    "message": result.get("message"),
                    "pending_edits": result.get("edits") or [],
                    "applied_edits": result.get("applied_edits") or [],
                    "active_path_leaf": result.get("active_path_leaf"),
                    "next_responder": result.get("next_responder"),
                }
            )
        except GeneratorExit:
            # Client aborted (Stop button / browser disconnect). Persist
            # whatever we have so the partial reply lives in the tree as a
            # regular message rather than being lost. Re-raise so Flask can
            # close the response cleanly.
            log.info("client aborted cid=%s after %.2fs", cid, time.monotonic() - started)
            persist_partial("aborted")
            raise

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Content-Type": "text/event-stream; charset=utf-8",
        "Connection": "keep-alive",
    }
    response = Response(stream_with_context(gen()), headers=headers)
    return response


@bp.post("/conversations/<cid>/messages/<mid>/continue")
@login_required
def continue_message(cid: str, mid: str):
    """Stream a continuation of an existing assistant message in place.

    The existing content is sent to the model as the start of an assistant
    turn; the streamed tokens are appended to the message and the message
    record is updated when generation completes.
    """
    conv = convs.load_conversation(cid)
    if not conv or mid not in conv["messages"]:
        return jsonify({"error": "not found"}), 404
    msg = conv["messages"][mid]
    if msg["persona"] == "user":
        return jsonify({"error": "cannot continue a user message"}), 400

    persona = msg["persona"]
    speaker_id = msg.get("speaker_id")
    settings = conv.get("settings", {}) or {}
    model = settings.get("ollama_model_override") or (
        current_app.config.get("ollama") or {}
    ).get("model")
    profile = (current_app.config.get("model_profiles") or {}).get(model) or {}
    sampling = {**profile, **(settings.get("sampling") or {})}
    enable_thinking = bool(settings.get("enable_thinking", False))

    # Auto stop strings will be filled in once we have the prompt.
    auto_stop_pending = True

    # Walk the path up to (and including) the parent of this message, then
    # append the existing content as a primed assistant turn so the model
    # continues from where it left off.
    chain = convs.path_to_root(conv, msg["parent_id"]) if msg.get("parent_id") else []
    fake_conv = {**conv, "active_path_leaf": msg["parent_id"]} if msg.get("parent_id") else conv
    base_prompt = assemble_prompt(fake_conv, persona, speaker_id=speaker_id)
    primed = list(base_prompt["messages"])
    primed.append({"role": "assistant", "content": msg.get("content") or ""})
    primed.append({"role": "user", "content": "(continue)"})

    if auto_stop_pending:
        auto_stop = list(base_prompt.get("stop") or [])
        if auto_stop:
            existing = list(sampling.get("stop") or [])
            sampling["stop"] = list({*existing, *auto_stop})

    log.info(
        "continue cid=%s mid=%s persona=%s model=%s msgs=%d",
        cid, mid, persona, model, len(primed),
    )

    def gen() -> Iterator[str]:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        started = time.monotonic()
        first_chunk_at: float | None = None
        persisted = {"done": False}

        def persist_continuation(reason: str) -> dict | None:
            if persisted["done"]:
                return None
            persisted["done"] = True
            new_content = "".join(content_parts)
            thinking_text = "".join(thinking_parts)
            if not new_content and not thinking_text:
                return None
            try:
                live_conv = convs.load_conversation(cid) or conv
                target = live_conv["messages"].get(mid)
                if target is None:
                    return None
                target["content"] = (target.get("content") or "") + new_content
                target["edited_at"] = int(time.time())
                if thinking_text:
                    meta = target.setdefault("metadata", {})
                    meta["thinking"] = (meta.get("thinking") or "") + thinking_text
                if reason != "complete":
                    target.setdefault("metadata", {})["interrupted"] = reason
                convs.save_conversation(live_conv)
                log.info(
                    "continue %s cid=%s mid=%s elapsed=%.2fs added=%dch",
                    reason, cid, mid, time.monotonic() - started, len(new_content),
                )
                return {"message": target, "active_path_leaf": live_conv["active_path_leaf"]}
            except Exception:
                log.exception("persist_continuation failed cid=%s mid=%s", cid, mid)
                return None

        yield ":" + " " * 2048 + "\n\n"
        yield _sse_event({"type": "start", "persona": persona, "speaker_id": speaker_id, "model": model, "continue": True})
        try:
            try:
                for ev in chat_stream(
                    system=base_prompt["system"],
                    messages=primed,
                    model=model,
                    options=sampling,
                    think=enable_thinking,
                ):
                    if first_chunk_at is None:
                        first_chunk_at = time.monotonic()
                        log.info("first chunk after %.2fs (kind=%s)", first_chunk_at - started, ev["kind"])
                    if ev["kind"] == "thinking":
                        thinking_parts.append(ev["text"])
                        yield _sse_event({"type": "thinking", "content": ev["text"]})
                    else:
                        content_parts.append(ev["text"])
                        yield _sse_event({"type": "delta", "content": ev["text"]})
            except Exception as e:
                log.exception("continue stream failed")
                persist_continuation("error")
                yield _sse_event({"type": "error", "error": str(e)})
                return

            result = persist_continuation("complete") or {}
            yield _sse_event({
                "type": "done",
                "message": result.get("message"),
                "active_path_leaf": result.get("active_path_leaf"),
                "continued": True,
            })
        except GeneratorExit:
            log.info("continue aborted cid=%s mid=%s after %.2fs", cid, mid, time.monotonic() - started)
            persist_continuation("aborted")
            raise

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Content-Type": "text/event-stream; charset=utf-8",
        "Connection": "keep-alive",
    }
    return Response(stream_with_context(gen()), headers=headers)


# ---------------------------------------------------------------------------
# Narrator-edit: rewrite a target message via a user directive.
#
# The user selects a message in the chat and types a directive describing
# what should be different ("swap Iris to a green cardigan", "move both
# to the reading nook"). The narrator gets a focused edit-mode prompt with the
# target body + directive + available outfit/room ids, and is asked to
# emit edit directives + a rewritten body.
#
# We stream the narrator's content as SSE deltas (so the user sees the
# rewrite happen live), then on completion: parse out the edits, apply
# them to the instance, append a NEW sibling message under the same
# parent as the target (so the original stays addressable via the normal
# sibling chip), store the directive + raw response in
# metadata.narrator_edit, and emit a "done" event with the new message.
# ---------------------------------------------------------------------------


@bp.post("/conversations/<cid>/narrator-edit/<mid>")
@login_required
def narrator_edit(cid: str, mid: str):
    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "conversation not found"}), 404
    if mid not in conv.get("messages", {}):
        return jsonify({"error": "target message not found"}), 404

    payload = request.get_json(silent=True) or {}
    directive = (payload.get("directive") or "").strip()
    if not directive:
        return jsonify({"error": "directive required"}), 400

    target = conv["messages"][mid]
    # Root messages have no parent; the rewrite is appended as a new
    # root (parent_id=None) instead of a sibling. The presence snapshot
    # falls back to the root's own snapshot since there's no parent
    # snapshot to inherit from.

    settings = conv.get("settings", {}) or {}
    model = settings.get("ollama_model_override") or (
        current_app.config.get("ollama") or {}
    ).get("model")
    profile = (current_app.config.get("model_profiles") or {}).get(model) or {}
    conv_sampling = settings.get("sampling") or {}
    sampling = {**profile, **conv_sampling}
    enable_thinking = bool(payload.get("think") or settings.get("enable_thinking", False))

    # Every narrator call uses the broader add prompt. Validated empirically:
    # the swap-outfit regression test confirmed pure-edit directives still
    # produce clean single-edit results (10/10 outfit_correct, 0/10 off-cast
    # contamination, 0/10 extra-chars modified) — the off-cast roster
    # doesn't distract the model when the directive is in-cast scoped.
    # The narrator_edit prompt + build_edit_prompt stay in the codebase
    # for now but nothing routes through them.
    from ..narrator_add import build_add_prompt
    prompt_kind = "narrator-add"

    try:
        prompt = build_add_prompt(conv, mid, directive)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    log.info(
        "%s cid=%s mid=%s model=%s sys=%dch directive=%s think=%s",
        prompt_kind, cid, mid, model, len(prompt["system"]), directive[:80], enable_thinking,
    )

    def gen() -> Iterator[str]:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        started = time.monotonic()
        first_chunk_at: float | None = None
        persisted = {"done": False}

        def persist(reason: str) -> dict | None:
            if persisted["done"]:
                return None
            persisted["done"] = True
            full_text = "".join(content_parts).strip()
            thinking_text = "".join(thinking_parts)
            if not full_text:
                return None
            new_body, edits = extract_edits(full_text)
            new_body = new_body.strip()

            try:
                live_conv = convs.load_conversation(cid) or conv
                if mid not in (live_conv.get("messages") or {}):
                    log.warning("%s target message %s vanished from cid=%s", prompt_kind, mid, cid)
                    return None
                # Single shared persistence path with the test harness:
                # see narrator_edit.append_narrator_edit_result.
                from ..narrator_edit import append_narrator_edit_result
                new_msg = append_narrator_edit_result(
                    live_conv, mid,
                    directive=directive,
                    raw_response=full_text,
                    new_body=new_body,
                    edits=edits,
                    thinking_text=thinking_text,
                    reason=reason,
                )
                applied_log = ((new_msg.get("metadata") or {})
                               .get("applied_edits") or [])
                log.info(
                    "%s %s cid=%s sibling_of=%s new=%s elapsed=%.2fs new_body=%dch edits=%d",
                    prompt_kind, reason, cid, mid, new_msg["id"],
                    time.monotonic() - started, len(new_body), len(edits),
                )
                return {
                    "message": new_msg,
                    "edits": edits,
                    "applied": applied_log,
                    "active_path_leaf": live_conv["active_path_leaf"],
                }
            except Exception:
                log.exception("%s persist failed cid=%s mid=%s", prompt_kind, cid, mid)
                return None

        yield ":" + " " * 2048 + "\n\n"
        yield _sse_event({"type": "start", "model": model, "target_mid": mid})

        try:
            try:
                for ev in chat_stream(
                    system=prompt["system"],
                    messages=prompt["messages"],
                    model=model,
                    options=sampling,
                    think=enable_thinking,
                ):
                    if first_chunk_at is None:
                        first_chunk_at = time.monotonic()
                    if ev["kind"] == "thinking":
                        thinking_parts.append(ev["text"])
                        yield _sse_event({"type": "thinking", "content": ev["text"]})
                    else:
                        content_parts.append(ev["text"])
                        yield _sse_event({"type": "delta", "content": ev["text"]})
            except Exception as e:
                log.exception("%s stream failed", prompt_kind)
                persist("error")
                yield _sse_event({"type": "error", "error": str(e)})
                return

            result = persist("complete") or {}
            yield _sse_event({
                "type": "done",
                "message": result.get("message"),
                "edits": result.get("edits") or [],
                "applied": result.get("applied") or [],
                "active_path_leaf": result.get("active_path_leaf"),
            })
        except GeneratorExit:
            log.info("%s aborted cid=%s mid=%s", prompt_kind, cid, mid)
            persist("aborted")
            raise

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Content-Type": "text/event-stream; charset=utf-8",
        "Connection": "keep-alive",
    }
    return Response(stream_with_context(gen()), headers=headers)


# ---------------------------------------------------------------------------
# Narrator-add streaming endpoint
#
# Sibling of /narrator-edit. The narrator is asked to introduce a NEW element
# into the scene — bring an off-cast character in, add a relationship note,
# add an object — instead of rewriting an existing message. Same return
# shape (sibling rewrite + edits + applied), so the persistence helpers,
# SSE format, and frontend stream handler are shared.
#
# Gated on settings.narrator_additions because it's an authoring-style
# capability (auto-instances off-cast characters into the conversation),
# distinct from the per-message edit affordance.
# ---------------------------------------------------------------------------


@bp.post("/conversations/<cid>/narrator-add/<mid>")
@login_required
def narrator_add(cid: str, mid: str):
    from ..narrator_add import build_add_prompt

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "conversation not found"}), 404
    if mid not in conv.get("messages", {}):
        return jsonify({"error": "target message not found"}), 404

    settings = conv.get("settings", {}) or {}

    payload = request.get_json(silent=True) or {}
    directive = (payload.get("directive") or "").strip()
    if not directive:
        return jsonify({"error": "directive required"}), 400

    model = settings.get("ollama_model_override") or (
        current_app.config.get("ollama") or {}
    ).get("model")
    profile = (current_app.config.get("model_profiles") or {}).get(model) or {}
    conv_sampling = settings.get("sampling") or {}
    sampling = {**profile, **conv_sampling}
    enable_thinking = bool(payload.get("think") or settings.get("enable_thinking", False))

    try:
        prompt = build_add_prompt(conv, mid, directive)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    log.info(
        "narrator-add cid=%s mid=%s model=%s sys=%dch directive=%s think=%s",
        cid, mid, model, len(prompt["system"]), directive[:80], enable_thinking,
    )

    def gen() -> Iterator[str]:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        started = time.monotonic()
        first_chunk_at: float | None = None
        persisted = {"done": False}

        def persist(reason: str) -> dict | None:
            if persisted["done"]:
                return None
            persisted["done"] = True
            full_text = "".join(content_parts).strip()
            thinking_text = "".join(thinking_parts)
            if not full_text:
                return None
            from ..narrator import extract_edits
            new_body, edits = extract_edits(full_text)
            new_body = new_body.strip()

            try:
                live_conv = convs.load_conversation(cid) or conv
                if mid not in (live_conv.get("messages") or {}):
                    log.warning("narrator-add target message %s vanished from cid=%s", mid, cid)
                    return None
                # Reuse the narrator-edit persistence helper. Same shape:
                # apply edits, append a sibling, write metadata.narrator_edit
                # + applied_edits, save. The auto-instance helper in
                # narrator_apply._record_edit fires inside apply_edits, so
                # off-cast characters land correctly here too.
                from ..narrator_edit import append_narrator_edit_result
                new_msg = append_narrator_edit_result(
                    live_conv, mid,
                    directive=directive,
                    raw_response=full_text,
                    new_body=new_body,
                    edits=edits,
                    thinking_text=thinking_text,
                    reason=reason,
                )
                applied_log = ((new_msg.get("metadata") or {})
                               .get("applied_edits") or [])
                log.info(
                    "narrator-add %s cid=%s sibling_of=%s new=%s elapsed=%.2fs new_body=%dch edits=%d",
                    reason, cid, mid, new_msg["id"],
                    time.monotonic() - started, len(new_body), len(edits),
                )
                return {
                    "message": new_msg,
                    "edits": edits,
                    "applied": applied_log,
                    "active_path_leaf": live_conv["active_path_leaf"],
                }
            except Exception:
                log.exception("narrator-add persist failed cid=%s mid=%s", cid, mid)
                return None

        yield ":" + " " * 2048 + "\n\n"
        yield _sse_event({"type": "start", "model": model, "target_mid": mid})

        try:
            try:
                for ev in chat_stream(
                    system=prompt["system"],
                    messages=prompt["messages"],
                    model=model,
                    options=sampling,
                    think=enable_thinking,
                ):
                    if first_chunk_at is None:
                        first_chunk_at = time.monotonic()
                    if ev["kind"] == "thinking":
                        thinking_parts.append(ev["text"])
                        yield _sse_event({"type": "thinking", "content": ev["text"]})
                    else:
                        content_parts.append(ev["text"])
                        yield _sse_event({"type": "delta", "content": ev["text"]})
            except Exception as e:
                log.exception("narrator-add stream failed")
                persist("error")
                yield _sse_event({"type": "error", "error": str(e)})
                return

            result = persist("complete") or {}
            yield _sse_event({
                "type": "done",
                "message": result.get("message"),
                "edits": result.get("edits") or [],
                "applied": result.get("applied") or [],
                "active_path_leaf": result.get("active_path_leaf"),
            })
        except GeneratorExit:
            log.info("narrator-add aborted cid=%s mid=%s", cid, mid)
            persist("aborted")
            raise

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Content-Type": "text/event-stream; charset=utf-8",
        "Connection": "keep-alive",
    }
    return Response(stream_with_context(gen()), headers=headers)


# ---------------------------------------------------------------------------
# Setup-from-directive (streaming)
#
# Body: {"directive": "<free text>"}. The narrator translates the directive
# into [outfit] / [move] / [set] directives + a fenced ```opening block,
# and the route persists the result as a new sibling root setup the user
# can navigate to via the existing root branch arrows.
# ---------------------------------------------------------------------------


@bp.post("/conversations/<cid>/setups/from-directive")
@login_required
def setup_from_directive(cid: str):
    from ..setup_from_directive import build_setup_prompt, parse_setup_response

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "conversation not found"}), 404

    payload = request.get_json(silent=True) or {}
    directive = (payload.get("directive") or "").strip()
    if not directive:
        return jsonify({"error": "directive required"}), 400

    settings = conv.get("settings", {}) or {}
    model = settings.get("ollama_model_override") or (
        current_app.config.get("ollama") or {}
    ).get("model")
    profile = (current_app.config.get("model_profiles") or {}).get(model) or {}
    conv_sampling = settings.get("sampling") or {}
    sampling = {**profile, **conv_sampling}
    enable_thinking = bool(payload.get("think") or settings.get("enable_thinking", False))

    prompt = build_setup_prompt(conv, directive)

    log.info(
        "setup-from-directive cid=%s model=%s sys=%dch directive=%s think=%s",
        cid, model, len(prompt["system"]), directive[:80], enable_thinking,
    )

    def gen() -> Iterator[str]:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        started = time.monotonic()
        persisted = {"done": False}

        def persist(reason: str) -> dict | None:
            if persisted["done"]:
                return None
            persisted["done"] = True
            full_text = "".join(content_parts).strip()
            thinking_text = "".join(thinking_parts)
            if not full_text:
                return None
            parsed = parse_setup_response(full_text)
            if not parsed["edits"] and not parsed["opening_prompt"]:
                log.warning(
                    "setup-from-directive cid=%s produced no edits or opening — full_text=%dch",
                    cid, len(full_text),
                )
            try:
                live_conv = convs.load_conversation(cid) or conv
                root = convs.seed_setup_root_from_directive(
                    live_conv,
                    name=parsed["name"] or "Directive setup",
                    description=directive[:160],
                    opening_prompt=parsed["opening_prompt"]
                        or parsed["leftover"]
                        or directive,
                    instructions=parsed["instructions"],
                    directive=directive,
                    edits=parsed["edits"],
                )
                root_meta = root.setdefault("metadata", {})
                root_meta["from_directive"] = {
                    "directive": directive,
                    "raw_response": full_text,
                    "thinking_trace": thinking_text,
                    "reason": reason,
                }
                convs.save_conversation(live_conv)
                log.info(
                    "setup-from-directive %s cid=%s new_root=%s edits=%d opening=%dch elapsed=%.2fs",
                    reason, cid, root["id"], len(parsed["edits"]),
                    len(parsed["opening_prompt"]),
                    time.monotonic() - started,
                )
                return {
                    "message": root,
                    "edits": parsed["edits"],
                    "opening_prompt": parsed["opening_prompt"],
                    "instructions": parsed["instructions"],
                    "name": parsed["name"],
                    "active_path_leaf": live_conv["active_path_leaf"],
                }
            except Exception:
                log.exception("setup-from-directive persist failed cid=%s", cid)
                return None

        yield ":" + " " * 2048 + "\n\n"
        yield _sse_event({"type": "start", "model": model, "directive": directive})

        try:
            try:
                for ev in chat_stream(
                    system=prompt["system"],
                    messages=prompt["messages"],
                    model=model,
                    options=sampling,
                    think=enable_thinking,
                ):
                    if ev["kind"] == "thinking":
                        thinking_parts.append(ev["text"])
                        yield _sse_event({"type": "thinking", "content": ev["text"]})
                    else:
                        content_parts.append(ev["text"])
                        yield _sse_event({"type": "delta", "content": ev["text"]})
            except Exception as e:
                log.exception("setup-from-directive stream failed")
                persist("error")
                yield _sse_event({"type": "error", "error": str(e)})
                return

            result = persist("complete") or {}
            yield _sse_event({
                "type": "done",
                "message": result.get("message"),
                "edits": result.get("edits") or [],
                "opening_prompt": result.get("opening_prompt", ""),
                "instructions": result.get("instructions", ""),
                "name": result.get("name", ""),
                "active_path_leaf": result.get("active_path_leaf"),
            })
        except GeneratorExit:
            log.info("setup-from-directive aborted cid=%s", cid)
            persist("aborted")
            raise

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Content-Type": "text/event-stream; charset=utf-8",
        "Connection": "keep-alive",
    }
    return Response(stream_with_context(gen()), headers=headers)


def _user_name_from_edits(edits: list[dict]) -> str:
    """Pull the `user.name` value out of a parsed edit list, for the
    branch label + opening line. Falls back to 'You'."""
    for e in edits or []:
        if e.get("kind") == "patch" and e.get("id") == "user":
            nm = ((e.get("data") or {}).get("name"))
            if isinstance(nm, str) and nm.strip():
                return nm.strip()
    return "You"


@bp.post("/conversations/<cid>/build-user")
@login_required
def build_user(cid: str):
    """Generate a custom branched user from a free-text self-description.

    Streams the narrator's `[set user.X = ...]` + `[outfit user -> ...]`
    directives, then seeds a new staging branch whose user persona is the
    generated structured entity (branch-scoped, like any setup). The
    staging panel can call this so a user can describe themselves instead
    of hand-filling the persona fields.
    """
    from ..user_build import build_user_prompt, parse_user_response

    conv = convs.load_conversation(cid)
    if not conv:
        return jsonify({"error": "conversation not found"}), 404

    payload = request.get_json(silent=True) or {}
    description = (payload.get("description") or "").strip()
    if not description:
        return jsonify({"error": "description required"}), 400

    settings = conv.get("settings", {}) or {}
    model = settings.get("ollama_model_override") or (
        current_app.config.get("ollama") or {}
    ).get("model")
    profile = (current_app.config.get("model_profiles") or {}).get(model) or {}
    conv_sampling = settings.get("sampling") or {}
    sampling = {**profile, **conv_sampling}
    enable_thinking = bool(payload.get("think") or settings.get("enable_thinking", False))

    prompt = build_user_prompt(conv, description)

    log.info(
        "build-user cid=%s model=%s sys=%dch desc=%s think=%s",
        cid, model, len(prompt["system"]), description[:80], enable_thinking,
    )

    def gen() -> Iterator[str]:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        started = time.monotonic()
        persisted = {"done": False}

        def persist(reason: str) -> dict | None:
            if persisted["done"]:
                return None
            persisted["done"] = True
            full_text = "".join(content_parts).strip()
            if not full_text:
                return None
            parsed = parse_user_response(full_text)
            if not parsed["edits"]:
                log.warning(
                    "build-user cid=%s produced no edits — full_text=%dch",
                    cid, len(full_text),
                )
            try:
                live_conv = convs.load_conversation(cid) or conv
                user_name = _user_name_from_edits(parsed["edits"])
                root = convs.seed_setup_root_from_directive(
                    live_conv,
                    name=f"Custom user: {user_name}",
                    description=description[:160],
                    opening_prompt=f"{user_name} steps into the scene.",
                    instructions="",
                    directive=description,
                    edits=parsed["edits"],
                )
                root_meta = root.setdefault("metadata", {})
                root_meta["from_user_build"] = {
                    "description": description,
                    "raw_response": full_text,
                    "thinking_trace": "".join(thinking_parts),
                    "reason": reason,
                }
                convs.save_conversation(live_conv)
                log.info(
                    "build-user %s cid=%s new_root=%s edits=%d elapsed=%.2fs",
                    reason, cid, root["id"], len(parsed["edits"]),
                    time.monotonic() - started,
                )
                return {
                    "message": root,
                    "edits": parsed["edits"],
                    "user_name": user_name,
                    "active_path_leaf": live_conv["active_path_leaf"],
                }
            except Exception:
                log.exception("build-user persist failed cid=%s", cid)
                return None

        yield ":" + " " * 2048 + "\n\n"
        yield _sse_event({"type": "start", "model": model, "description": description})

        try:
            try:
                for ev in chat_stream(
                    system=prompt["system"],
                    messages=prompt["messages"],
                    model=model,
                    options=sampling,
                    think=enable_thinking,
                ):
                    if ev["kind"] == "thinking":
                        thinking_parts.append(ev["text"])
                        yield _sse_event({"type": "thinking", "content": ev["text"]})
                    else:
                        content_parts.append(ev["text"])
                        yield _sse_event({"type": "delta", "content": ev["text"]})
            except Exception as e:
                log.exception("build-user stream failed")
                persist("error")
                yield _sse_event({"type": "error", "error": str(e)})
                return

            result = persist("complete") or {}
            yield _sse_event({
                "type": "done",
                "message": result.get("message"),
                "edits": result.get("edits") or [],
                "user_name": result.get("user_name", ""),
                "active_path_leaf": result.get("active_path_leaf"),
            })
        except GeneratorExit:
            log.info("build-user aborted cid=%s", cid)
            persist("aborted")
            raise

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Content-Type": "text/event-stream; charset=utf-8",
        "Connection": "keep-alive",
    }
    return Response(stream_with_context(gen()), headers=headers)
