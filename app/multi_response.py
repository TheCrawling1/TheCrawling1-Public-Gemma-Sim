"""Multi-response: when triggering a character response, optionally
have all other characters in the same room also react.

Single LLM call: append `build_joint_directive(...)` to the lead's
system prompt (telling the model this turn is multi-character and
asking for an interleaved scene), drop every per-character
`\n<Name>:` stop (via `multi_stop_list` — both partners' and
co-present non-partners', keeping only the user handback) so the
stream produces the whole scene in one go, then split the buffered
output into per-speaker bodies via `split_joint_n` and persist each
partner chained under the previous speaker.

Aliases: characters with multi-word display names (e.g. Priya =
"Dr. Priya Anand") routinely have models label their beats with
a shorter form ("Priya:", "Dr. Anand:") that the canonical name alone
won't match. Each character carries a list of acceptable labels —
the canonical display name plus any `properties.aliases` declared
in the character JSON plus auto-derived title-stripped variants —
and both the split regex and the streaming router try every one.
Bodies are keyed by canonical so the rest of the pipeline doesn't
care which alias the model used.

Validated empirically (see tools/test_multi_response_idols.py at
N=6: 10/10 rolls, all six idols spoke, with cross-character
references like one character calling another by name) — single
call beats the prior two-call lead_then_extend approach on both
speed (~20s vs ~30s) and cohesion (interleaved vs. isolated
paragraphs).
"""
from __future__ import annotations

import re
from typing import Any

from .effective import effective_cast_at, effective_entities_at
from .entities import load_instance_entities


_TITLE_PREFIX_RE = re.compile(r"^(Dr|Mr|Mrs|Ms|Sgt|Lt|Capt|Cpt|Pvt)\.?\s+", re.IGNORECASE)


def multi_stop_list(
    existing_stop: list[str] | None,
    full_stop: list[str] | None,
    user_only_stop: list[str] | None,
) -> list[str]:
    """Stop list for a joint multi-speaker generation.

    A multi turn is ONE LLM call that voices the lead plus every partner
    back-to-back (``split_joint_n`` partitions the output afterwards, and
    ``num_predict`` bounds length). Any per-character ``\\n<Name>:`` stop
    left armed mid-scene truncates the whole thing — the historical
    multi-speaker bug, where a co-present but non-partner character's label
    (Nadia / Milo) killed the stream right after the lead's opener, leaving the
    partner bodies empty and forcing a standalone top-up (short lead, long
    partner).

    So we drop EVERY auto-generated character label (``full_stop`` minus
    the user-handback ``user_only_stop``) — both partners' and co-present
    non-partners' — while keeping the user handback (so a runaway into the
    user's turn still halts) and any custom conv/profile stops the user
    configured (anything in ``existing_stop`` that isn't an auto character
    label).
    """
    char_stops = set(full_stop or ()) - set(user_only_stop or ())
    return [s for s in (existing_stop or []) if s not in char_stops]


def aliases_for(character: dict[str, Any] | None) -> list[str]:
    """Return the list of label strings the split should accept for
    this character — canonical display name first, then any declared
    aliases under ``properties.aliases``, then auto-derived
    title-stripped variants. Order matters only for documentation;
    `split_joint_n` sorts by length-desc before building the regex so
    longer names win when one alias is a prefix of another.
    """
    if not isinstance(character, dict):
        return []
    name = character.get("name") or character.get("id") or ""
    if not isinstance(name, str) or not name:
        return []
    out: list[str] = [name]
    props = character.get("properties") or {}
    declared = props.get("aliases")
    if isinstance(declared, list):
        for a in declared:
            if isinstance(a, str) and a and a not in out:
                out.append(a)
    # Auto-strip honorifics — "Dr. Priya Anand" → "Priya Anand".
    stripped = _TITLE_PREFIX_RE.sub("", name).strip()
    if stripped and stripped != name and stripped not in out:
        out.append(stripped)
    # First- and last-name short forms — a model labelling a beat
    # "Rika:" for "Rika Jougasaki:" is the dominant real-world failure
    # (see docs/multi_response_investigation.md: 10/10 rolls used the
    # first name, never the canonical). Derive both tokens of a
    # multi-word (title-stripped) name so the split routes them.
    # Collisions between two characters sharing a token are resolved at
    # the split level (ambiguous labels are dropped), so it's safe to
    # offer both here.
    tokens = [t for t in re.split(r"\s+", stripped or name) if t]
    if len(tokens) >= 2:
        for tok in (tokens[0], tokens[-1]):
            clean = tok.strip(".,'’-")
            if len(clean) >= 2 and clean not in out:
                out.append(clean)
    return out


def is_enabled(conversation: dict[str, Any]) -> bool:
    s = conversation.get("settings") or {}
    return bool(s.get("multi_response"))


def _excluded_ids(conversation: dict[str, Any]) -> set[str]:
    s = conversation.get("settings") or {}
    return {x for x in (s.get("multi_response_excluded") or []) if isinstance(x, str)}


def partners_for_lead(
    conversation: dict[str, Any],
    lead_id: str,
    leaf_id: str | None = None,
) -> list[str]:
    """Resolve which characters react alongside `lead_id`.

    Returns character ids in the same room as the lead, minus the
    lead, minus anyone in `settings.multi_response_excluded`, minus
    anyone who is not in the **branch's effective cast** (i.e., who's
    been removed from this branch via a ``cast_remove`` edit, or who
    was never picked into the staging branch in the first place).
    Order follows the conversation's ``turn_order`` so sequence is
    deterministic.

    Same-room is intentional: the multi-response should mirror what
    the model's prompt thinks is "in scene with you" — ``_others_present_text``
    in personas.py also filters by room. If the user wants characters
    in different rooms to react, the right move is to stage them into
    the same room first (a narrator edit), not to relax this filter.

    Falls back to "all branch-cast characters minus excluded" when the
    lead has no room data in the leaf's presence_snapshot. The cast
    filter still applies in that fallback — without it, a fresh staging
    branch where the lead's room hadn't been set yet would pull every
    scenario character into the response, ignoring the user's pick.
    """
    leaf_id = leaf_id or conversation.get("active_path_leaf") or ""
    msgs = conversation.get("messages") or {}
    leaf_msg = msgs.get(leaf_id) or {}
    snap = (leaf_msg.get("presence_snapshot") or {}).get("presence") or {}

    settings = conversation.get("settings") or {}
    turn_order = list(settings.get("turn_order") or [])
    excl = _excluded_ids(conversation)
    entities = effective_entities_at(conversation, leaf_id)
    # Branch-scoped cast: a character cast_removed on this path (or
    # never cast_added into a staging branch) shouldn't be pulled into
    # the multi-response even if they're still in turn_order and the
    # shared instance pool.
    branch_cast = effective_cast_at(conversation, leaf_id).get("characters") or set()

    lead_room = (snap.get(lead_id) or {}).get("room")

    out: list[str] = []
    for cid in turn_order:
        if cid == lead_id or cid in excl:
            continue
        if cid not in branch_cast:
            continue
        ent = entities.get(cid)
        if not ent or ent.get("type") != "character":
            continue
        if lead_room:
            char_room = (snap.get(cid) or {}).get("room")
            if char_room != lead_room:
                continue
        out.append(cid)
    return out


def normalize_inline_labels(
    raw: str,
    labels_by_canonical: dict[str, list[str]] | list[str],
) -> str:
    """Inject a newline before any `<Label>: "` that appears mid-line so
    `split_joint_n` can attribute it to the right speaker.

    Failure mode this catches: model writes a screenplay-style run-on
    like `*Iris steadies the ladder.* "Hold still." Milo wobbles, "I'm trying!"`
    inside Iris's block. The label `Milo:` (if used) would be
    line-anchored by the split regex and missed; here we don't even
    have a colon — but a more common variant the model emits is
    `... Milo: "I'm trying!"` inline. We rewrite to put `Milo:` at
    line start so the downstream split routes the dialogue correctly.

    Conservative on purpose: requires a colon followed by a double-quote
    within 4 chars so we only move clear dialogue-attribution patterns,
    not narrative name mentions ("she turns to Dex").
    """
    if not raw:
        return raw
    if isinstance(labels_by_canonical, list):
        labels_by_canonical = {n: [n] for n in labels_by_canonical}
    all_labels: list[str] = []
    for labels in labels_by_canonical.values():
        for lbl in labels:
            if isinstance(lbl, str) and lbl:
                all_labels.append(lbl)
    if not all_labels:
        return raw
    all_labels.sort(key=len, reverse=True)
    name_alt = "|".join(re.escape(lbl) for lbl in all_labels)
    # Match `<Label>:\s*"` not preceded by a newline. The lookbehind on
    # `[^\n]` keeps already-line-anchored labels alone.
    pattern = re.compile(
        rf"(?<=[^\n])([ \t]*)({name_alt})([\s\*_]*:[\s\*_]*)(?=\")",
        re.IGNORECASE,
    )
    return pattern.sub(lambda m: "\n" + m.group(2) + m.group(3), raw)


# A capitalized 1-3 word label sitting alone on its own line, ending in
# a colon — the shape the joint directive asks for and the shape models
# actually emit. Used to catch UNRECOGNIZED labels (a wrong/hallucinated
# name) so they still end the current block and become a positional-fill
# candidate, instead of gluing the next speaker's content + raw label
# into the previous body. Own-line-only (trailing `\n`) keeps it from
# firing on inline `"Wait:"` dialogue or mid-prose colons.
_GENERIC_LABEL_BODY = r"[A-Z][A-Za-z'’\-]+(?:[ \t]+[A-Z][A-Za-z'’\-]+){0,2}"

# 1-4 capitalized name-shaped words. Used by the colon-LESS numbered
# label branch (`3. Kozue Kousaka\n` — observed in the replay harness
# at history depth, where the model keeps the number + name but drops
# the colon). Requiring every token capitalized + the label alone on
# its line keeps it off real numbered prose ("1. dance practice",
# "1. Wait, the prompt…").
_NAME_WORDS = r"[A-Z][A-Za-z'’.\-]*(?:[ \t]+[A-Z][A-Za-z'’.\-]+){0,3}"


# Meta-commentary detection — the model's planning monologue leaking
# into the content stream as if it were prose. Observed in production
# (conv_45f110bb860e): a partner block opening with
# `*Wait, the prompt didn't specify Risa, but mentioned "the girls" …
# I will include the others present …*` before the actual in-character
# beat. With think=off the reasoning has nowhere else to go, and the
# multi directive's "open with *" rule gets it wrapped like an action.
#
# Patterns are deliberately phrase-level (not bare words) so real
# roleplay prose survives: "she prompts him to sit" is fine, "the
# prompt didn't specify" is not.
_META_PATTERNS = [
    # "the prompt" = the LLM instruction. Exclude a physical "prompt
    # sheet / card / paper / page / board / book", which is in-world
    # scene furniture (a writing club runs on poetry prompts).
    r"\bthe (?:user'?s? )?prompt\b(?!\s+(?:sheet|card|paper|page|board|book|list))",
    r"\bthis prompt\b",
    r"\bthe (?:system|user) (?:prompt|message|instruction)s?\b",
    r"\bthe directives?\b",
    r"\bthe roster\b",
    r"\bspeaker labels?\b",
    r"\blabell?ed blocks?\b",
    r"\bI(?: wi|')ll (?:include|write|voice|respond|continue)\b",
    r"\bI will (?:include|write|voice|respond|continue|now)\b",
    r"\bthe user'?s? (?:action|message|turn|input)\b",
    r"\bprevious turn(?:'s)?\b",
    r"\bcontext of the previous\b",
    r"\bas an AI\b",
    r"\blanguage model\b",
    r"\bstay(?:ing)? in character\b",
]
_META_RE = re.compile("|".join(_META_PATTERNS), re.IGNORECASE)

# Tier 2 — planning-voice paragraphs. Replay run 3 showed the model
# spiralling on quote-free decision chatter that dodges the phrase
# patterns above: `*Wait, Risa isn't here.*`, `*Actually, let's just
# use Risa.*`, `(Let's go.)`, `(Ok.)` … repeated dozens of times.
# A paragraph counts as planning voice when it contains NO quoted
# dialogue (real beats carry "quotes"; pure third-person action beats
# don't open with these markers) and, once stripped of asterisk /
# underscore / parenthesis wrapping, starts with a deliberation marker.
_PLANNING_OPENER_RE = re.compile(
    r"^(?:wait\b|actually[,\s]|correction\b|correcting\s+for\b|no,\s+let'?s\b|"
    r"ok(?:ay)?[.,!]?\s*$|sigh,\s|let'?s\s|"
    r"i(?:'| wi)ll\s+(?:just\s+)?(?:use|write|continue|proceed|respond)\b)",
    re.IGNORECASE,
)


def _is_planning_voice(paragraph: str) -> bool:
    if '"' in paragraph or "“" in paragraph:
        return False  # contains dialogue — never strip real lines
    core = paragraph.strip().strip("*_()[]").strip()
    return bool(core) and bool(_PLANNING_OPENER_RE.match(core))


def looks_meta(paragraph: str) -> bool:
    """True when a paragraph reads as out-of-character planning / prompt
    commentary rather than scene prose."""
    if not paragraph:
        return False
    # A paragraph carrying in-character dialogue is scene prose, not
    # leaked OOC planning: genuine meta ("as an AI", "the directive
    # says", "I'll voice the roster:") never contains spoken lines. This
    # exemption also stops one stray flagged word inside a real beat —
    # e.g. an in-world "prompt sheet" in a writing-club scene — from
    # nuking the ENTIRE quoted beat (a beat is often a single paragraph,
    # so a wholesale drop loses the whole speaker). Mirrors the
    # planning-voice tier, which already exempts quoted paragraphs.
    if '"' in paragraph or "“" in paragraph or "”" in paragraph:
        return False
    return bool(_META_RE.search(paragraph)) or _is_planning_voice(paragraph)


def strip_meta_commentary(body: str) -> str:
    """Remove out-of-character planning paragraphs from a speaker body.

    Paragraph-level (blank-line separated): a paragraph containing a
    meta marker — or reading as quote-free planning voice ("Wait, …",
    "Actually, let's just …") — is dropped wholesale; everything else
    passes through untouched. Designed to remove ONLY leaked reasoning,
    not in-character prose: dialogue lines are exempt from the
    planning-voice tier, so a character can still SAY "Let's just get
    this over with." Returns "" when nothing survives (the caller then
    treats the speaker as silent, same as any empty body).
    """
    if not body:
        return body
    kept = [p for p in re.split(r"\n\s*\n", body) if not looks_meta(p)]
    out = "\n\n".join(p for p in kept if p.strip()).strip()
    return out


def split_joint_n(
    raw: str,
    labels_by_canonical: dict[str, list[str]] | list[str],
    *,
    positional_fallback: bool = True,
) -> tuple[dict[str, str], list[str]]:
    """Split a multi-speaker LLM output into per-speaker bodies.

    `labels_by_canonical` either:
      - dict[canonical_name, list[acceptable_labels]] — each speaker
        carries 1+ acceptable label strings (canonical + aliases). The
        dict's key order is the **expected speaker order** (lead first,
        then partners in turn order) — used by the positional fallback.
      - list[str] — legacy shape, each name is its own canonical.

    Two-layer attribution:

      1. **Named** — every `<Label>:` boundary (known alias, permissive;
         or any own-line capitalized label, strict) splits the text into
         blocks. Blocks whose label matches a known alias are assigned to
         that canonical. The label text is always consumed (never left in
         a body), so a wrong/hallucinated label can't leak as raw text.
      2. **Positional fallback** (`positional_fallback=True`) — blocks
         with an *unrecognized* label are zipped, in document order, onto
         the still-empty expected speakers, in declared order. This is the
         lever that recovers a roster the model labelled with the wrong
         names entirely (the dominant failure — see the investigation
         doc). Leftover unknown blocks (more than missing slots) and any
         `Narrator:` block fold into the preceding speaker so no content
         and no raw label is dropped on the floor.

    Returns (bodies, order): bodies maps each canonical to its body (""
    if silent); order is canonicals in first-appearance order.
    """
    # Normalize input shape to canonical → [labels].
    if isinstance(labels_by_canonical, list):
        labels_by_canonical = {n: [n] for n in labels_by_canonical}
    canonical_order = list(labels_by_canonical.keys())
    bodies: dict[str, str] = {n: "" for n in canonical_order}
    order: list[str] = []
    if not raw or not raw.strip() or not canonical_order:
        return bodies, order

    # Flatten aliases → canonical, lowercased. Ambiguous labels (one form
    # that two characters both accept, e.g. a shared surname) are dropped
    # so they never misroute — better unattributed-then-positional than
    # wrong.
    canonical_by_lower: dict[str, str] = {}
    ambiguous: set[str] = set()
    for canonical, labels in labels_by_canonical.items():
        for lbl in labels:
            if not (isinstance(lbl, str) and lbl):
                continue
            low = lbl.lower()
            if low in canonical_by_lower and canonical_by_lower[low] != canonical:
                ambiguous.add(low)
            else:
                canonical_by_lower[low] = canonical
    for low in ambiguous:
        canonical_by_lower.pop(low, None)
    known_labels = sorted(canonical_by_lower.keys(), key=len, reverse=True)
    if not known_labels:
        bodies[canonical_order[0]] = _restore_missing_opening_asterisk(
            strip_meta_commentary(raw.strip()))
        return bodies, [canonical_order[0]]

    # One regex, three branches at each line-start boundary:
    #   known — recognized alias, permissive post-colon (same-line OK,
    #           tolerates markdown/emphasis wrappers);
    #   num   — a roster-numbered label (`2. Rika Jougasaki:` — the
    #           directive's requested form). The NUMBER routes by roster
    #           index, so even a hallucinated name after it attributes
    #           correctly; a known name after the number wins over a
    #           contradicting number.
    #   gen   — any other capitalized own-line label (strict: must end
    #           the line) so an unknown name still cuts a block.
    name_alt = "|".join(re.escape(lbl) for lbl in known_labels)
    pattern = re.compile(
        rf"(?:\A|\n)[ \t]*[\*_>#\-]*"
        rf"(?:(?P<known>{name_alt})[\s\*_]*:(?:[ \t]*\n|[\s\*_]*)"
        rf"|(?P<num>\d{{1,2}})[\.\)][ \t]*(?P<numname>[^\n:]{{0,48}}?)[ \t]*[\*_]*:(?:[ \t]*\n|[ \t]*)"
        rf"|(?P<numnc>\d{{1,2}})[\.\)][ \t]+(?P<numncname>{_NAME_WORDS})[ \t]*\n"
        rf"|(?P<gen>{_GENERIC_LABEL_BODY})[ \t]*[\*_]*:[ \t]*(?:\n|$))",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(raw))
    if not matches:
        bodies[canonical_order[0]] = _restore_missing_opening_asterisk(
            strip_meta_commentary(raw.strip()))
        return bodies, [canonical_order[0]]

    # Build an ordered segment list: each is (kind, canonical, body).
    #   kind="lead"    unlabelled preamble → the lead
    #   kind="known"   recognized label (by name, recurrence, or number)
    #   kind="unknown" unrecognized own-line label → positional candidate
    #   kind="drop"    a Narrator: label → fold into previous speaker
    #
    # `seen_unknown` is the recurrence memory: the same unknown surface
    # name always maps to the same canonical. Without it, a model that
    # numbers dialogue TURNS instead of roster slots (observed run 4:
    # "2. Rika Hoshimiya: … 4. Rika Hoshimiya: …") sends the same
    # character's second beat to whoever owns roster slot 4.
    segments: list[dict[str, Any]] = []
    seen_unknown: dict[str, str] = {}
    if matches[0].start() > 0:
        preamble = raw[: matches[0].start()].strip()
        if preamble:
            segments.append({"kind": "lead", "canonical": canonical_order[0],
                             "body": _restore_missing_opening_asterisk(
                                 strip_meta_commentary(preamble))})
    for i, m in enumerate(matches):
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = _restore_missing_opening_asterisk(
            strip_meta_commentary(raw[m.end():body_end].strip()))
        if m.group("known"):
            segments.append({"kind": "known",
                             "canonical": canonical_by_lower[m.group("known").lower()],
                             "body": body})
        elif m.group("num") or m.group("numnc"):
            numname = (m.group("numname") or m.group("numncname") or "").strip()
            numval = m.group("num") or m.group("numnc")
            # Precedence: known alias > recurrence of this surface name >
            # roster index from the number.
            canonical = canonical_by_lower.get(numname.lower())
            if canonical is None and numname:
                canonical = seen_unknown.get(numname.lower())
            if canonical is None:
                idx = int(numval) - 1
                if 0 <= idx < len(canonical_order):
                    canonical = canonical_order[idx]
                    if numname:
                        seen_unknown[numname.lower()] = canonical
            if canonical is not None:
                segments.append({"kind": "known", "canonical": canonical, "body": body})
            elif numname.lower() == "narrator":
                segments.append({"kind": "drop", "canonical": None, "body": body})
            else:
                segments.append({"kind": "unknown", "canonical": None,
                                 "label": numname, "body": body})
        else:
            label = (m.group("gen") or "").strip()
            if label.lower() == "narrator":
                segments.append({"kind": "drop", "canonical": None, "body": body})
            else:
                segments.append({"kind": "unknown", "canonical": None,
                                 "label": label, "body": body})

    # Layer 1 — assign lead + known.
    claimed: set[str] = set()
    for seg in segments:
        if seg["kind"] in ("lead", "known"):
            seg["owner"] = seg["canonical"]
            claimed.add(seg["canonical"])

    # Layer 2 — positional fill: still-empty expected speakers, in
    # declared order, take the unknown-labelled blocks in document
    # order — grouped by surface label, so every block the model gave
    # the same (wrong) name lands on the same canonical.
    if positional_fallback:
        missing = [c for c in canonical_order if c not in claimed]
        unknown_segs = [s for s in segments if s["kind"] == "unknown"]
        mi = 0
        for seg in unknown_segs:
            label_low = (seg.get("label") or "").lower()
            prior = seen_unknown.get(label_low) if label_low else None
            if prior is not None:
                seg["owner"] = prior
                continue
            if mi < len(missing):
                seg["owner"] = missing[mi]
                claimed.add(missing[mi])
                if label_low:
                    seen_unknown[label_low] = missing[mi]
                mi += 1

    # Anything still unowned (leftover unknown blocks, narrator drops, or
    # unknowns when positional_fallback is off) folds into the previous
    # owned segment so its content survives and its label stays stripped.
    last_owner = canonical_order[0]
    for seg in segments:
        if seg.get("owner"):
            last_owner = seg["owner"]
        else:
            seg["owner"] = last_owner

    # Concatenate in document order, keyed by owner.
    for seg in segments:
        owner = seg["owner"]
        body = seg["body"]
        if not body:
            continue
        bodies[owner] = (bodies[owner] + "\n\n" + body) if bodies[owner] else body
        if owner not in order:
            order.append(owner)
    return bodies, order


def _restore_missing_opening_asterisk(body: str) -> str:
    """Defensive post-fix: when a per-character block opens with bare
    prose but contains at least one `*` somewhere in the body, prepend
    a `*` at the very start so the action paragraph is wrapped on both
    sides.

    The model sometimes drops the opening `*` of a partner block while
    still emitting the closing one — leaves the first action unwrapped
    and the closing `*` orphaned mid-paragraph. The directive's
    "Opening rule" instructs the model to lead with `*`; this is the
    catch when the model ignores that rule.

    Idempotent — if the body already opens with `*` (after leading
    whitespace), returns unchanged. If the body contains zero `*`,
    returns unchanged (we don't want to wrap a pure-dialogue body
    that's already correctly format-free).
    """
    if not body:
        return body
    stripped = body.lstrip()
    if not stripped or stripped.startswith("*") or "*" not in stripped:
        return body
    leading_ws_len = len(body) - len(stripped)
    return body[:leading_ws_len] + "*" + stripped


class JointStreamRouter:
    """Streams multi-speaker model output into per-speaker chunks.

    Maintains a small lookahead buffer; when a `\\n<Label>:` (or `\\A<Label>:`)
    boundary appears, switches the current speaker and drops the label.
    Use ``feed(text)`` per chunk and ``flush()`` at end-of-stream; both
    return ``[(speaker_id, content), ...]`` ready to forward to clients.

    Each partner carries `(speaker_id, [accepted_labels])` so models
    that label a beat with an alias (`Priya:` for `Dr. Priya Anand:`)
    still route to the right bubble during streaming.

    Keep the model's full raw output separately for the post-stream
    ``split_joint_n`` parse — the router strips labels and could miss
    boundaries that span chunks if the buffer's safety margin is wrong;
    persistence stays authoritative.
    """

    def __init__(
        self,
        lead_speaker_id: str,
        lead_labels: list[str],
        partners: list[tuple[str, list[str]]],
    ) -> None:
        self.current = lead_speaker_id
        # Build a flat label→speaker_id lookup. Every accepted label
        # for every speaker maps back to that speaker's id.
        self.label_to_id: dict[str, str] = {}
        for lbl in lead_labels:
            if isinstance(lbl, str) and lbl:
                self.label_to_id[lbl.lower()] = lead_speaker_id
        for sid, labels in partners:
            for lbl in labels:
                if isinstance(lbl, str) and lbl:
                    self.label_to_id[lbl.lower()] = sid
        self.max_name_len = max((len(n) for n in self.label_to_id.keys()), default=0)
        self.pending = ""
        self._at_start = True
        # Positional advance — mirrors split_joint_n's positional
        # fallback for the live stream: when the model labels a block
        # with a name we don't know (the dominant failure: hallucinated
        # roster names), the boundary still switches the speaker — to
        # the next expected speaker, in declared order, who hasn't
        # spoken yet. Keeps the live bubbles aligned with what the
        # post-stream split will persist for the common case (known
        # labels, when present, come in roster order). `Narrator:`
        # boundaries keep the current speaker (the split folds narrator
        # text into the previous block the same way).
        self._expected = [lead_speaker_id, *[sid for sid, _ in partners]]
        self._spoke: set[str] = {lead_speaker_id}
        # Recurrence memory for unknown surface names — the same wrong
        # name always routes to the same speaker (mirrors split_joint_n).
        self._seen_unknown: dict[str, str] = {}
        # Sort labels by length-desc when building the alternation so
        # longer aliases match before shorter substrings of them.
        sorted_labels = sorted(self.label_to_id.keys(), key=len, reverse=True)
        names_alt = "|".join(re.escape(lbl) for lbl in sorted_labels)
        # Same relaxation as split_joint_n: tolerate markdown emphasis
        # (* _) and list/quote/heading prefixes (> # -) around the
        # speaker label so the streaming router doesn't silently
        # misattribute content when the model decorates names.
        # Post-colon alternation mirrors split_joint_n: protect the
        # next line's first character (the action-opening `*`) when
        # the label is on its own line, but keep the legacy greedy
        # match for same-line labels and markdown-emphasis wrappers.
        # The `num` branch is the roster-numbered label the directive
        # requests (`2. Rika Jougasaki:`) — the number routes by roster
        # index even when the name after it is hallucinated; a known
        # name wins over a contradicting number. The `gen` branch is
        # the unknown-label boundary: any other capitalized own-line
        # `Word:` (strict — must end its line, so inline dialogue
        # colons don't trigger it). The leading decoration class is
        # HORIZONTAL-ONLY ([ \t], not \s): with \s it could swallow a
        # `\n` and the closing `*` of the previous line at a chunk seam
        # (`…*waves*` + `\n Mika:` buffered as `*\nMika:` matches \A +
        # class-eats-`*\n`), corrupting the previous speaker's last
        # action beat.
        self._boundary_re = re.compile(
            rf"(?:\A|\n)[ \t\*_>#\-]*"
            rf"(?:(?P<known>{names_alt})[\s\*_]*:(?:[ \t]*\n|[\s\*_]*)"
            rf"|(?P<num>\d{{1,2}})[\.\)][ \t]*(?P<numname>[^\n:]{{0,48}}?)[ \t]*[\*_]*:(?:[ \t]*\n|[ \t]*)"
            rf"|(?P<numnc>\d{{1,2}})[\.\)][ \t]+(?P<numncname>{_NAME_WORDS})[ \t]*\n"
            rf"|(?P<gen>{_GENERIC_LABEL_BODY})[ \t]*[\*_]*:[ \t]*\n)",
            re.IGNORECASE,
        )

    def _next_expected(self) -> str | None:
        for sid in self._expected:
            if sid not in self._spoke:
                return sid
        return None

    def feed(self, text: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self.pending += text
        while True:
            m = self._boundary_re.search(self.pending)
            if m:
                before = self.pending[: m.start()]
                if before:
                    out.append((self.current, before))
                if m.group("known"):
                    self.current = self.label_to_id[m.group("known").lower()]
                    self._spoke.add(self.current)
                elif m.group("num") or m.group("numnc"):
                    numname = (m.group("numname") or m.group("numncname") or "").strip()
                    numval = m.group("num") or m.group("numnc")
                    # Precedence mirrors split_joint_n: known alias >
                    # recurrence of this surface name > roster index.
                    sid = self.label_to_id.get(numname.lower())
                    if sid is None and numname:
                        sid = self._seen_unknown.get(numname.lower())
                    if sid is None:
                        idx = int(numval) - 1
                        if 0 <= idx < len(self._expected):
                            sid = self._expected[idx]
                            if numname:
                                self._seen_unknown[numname.lower()] = sid
                    if sid is not None:
                        self.current = sid
                        self._spoke.add(sid)
                    elif numname.lower() != "narrator":
                        nxt = self._next_expected()
                        if nxt is not None:
                            self.current = nxt
                            self._spoke.add(nxt)
                            if numname:
                                self._seen_unknown[numname.lower()] = nxt
                else:
                    label = (m.group("gen") or "").strip()
                    if label.lower() != "narrator":
                        # Unknown label → recurrence first, then
                        # positional advance. If every expected speaker
                        # already spoke, stay put (the split folds the
                        # leftover the same way).
                        sid = self._seen_unknown.get(label.lower())
                        if sid is not None:
                            self.current = sid
                        else:
                            nxt = self._next_expected()
                            if nxt is not None:
                                self.current = nxt
                                self._spoke.add(nxt)
                                self._seen_unknown[label.lower()] = nxt
                self.pending = self.pending[m.end():]
                self._at_start = True
                continue
            # No boundary in buffer. Decide what's safe to flush; hold
            # any tail that could still complete a partial boundary.
            if self._at_start and self._start_could_match(self.pending):
                break  # whole buffer might still be a boundary at \A
            self._at_start = False
            # Hold-back threshold covers the longest known alias AND a
            # potential unknown three-word label still being streamed.
            threshold = max(self.max_name_len + 4, 40)
            tail_search_from = max(0, len(self.pending) - threshold)
            nl = self.pending.rfind("\n", tail_search_from)
            if nl < 0:
                if self.pending:
                    out.append((self.current, self.pending))
                    self.pending = ""
                break
            if nl > 0:
                out.append((self.current, self.pending[:nl]))
            self.pending = self.pending[nl:]
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self.pending:
            return []
        out = [(self.current, self.pending)]
        self.pending = ""
        return out

    def _start_could_match(self, s: str) -> bool:
        # Strip the same leading decorations the boundary regex
        # tolerates (whitespace + markdown emphasis + list/quote/heading
        # prefixes). Without this, partial buffers like `**Mon` would
        # fail the prefix check and get flushed to the wrong speaker
        # before the colon arrives.
        body = s.lstrip(" \t*_>#-")
        if not body:
            return True
        if ":" in body:
            # A *known* label would already have matched (its post-colon
            # is permissive). An unknown label needs colon + newline —
            # hold when the colon ends the buffer so the `\n` can arrive.
            return bool(re.fullmatch(
                rf"{_GENERIC_LABEL_BODY}[ \t]*[\*_]*:[ \t]*", body
            ))
        body_lower = body.lower()
        for name in self.label_to_id.keys():
            if name.startswith(body_lower) or body_lower.startswith(name):
                return True
        # Could the buffer still grow into an unknown `Word Word Word:`
        # label or a numbered `2. Name:` label? Hold while it's a
        # plausible partial — prose breaks the pattern within a few
        # characters and flushes normally.
        return bool(re.fullmatch(
            r"[A-Z][A-Za-z'’\-]*(?:[ \t]+[A-Za-z'’\-]*){0,2}", body
        ) or re.fullmatch(
            r"\d{1,2}(?:[\.\)][ \t]*(?:[A-Za-z'’\- \t]{0,48})?)?", body
        ))


def build_joint_directive(lead_name: str, partner_names: list[str]) -> str:
    """Multi-character system-prompt directive: tells the model that
    this turn is a multi-character beat, names every partner, asks
    for an interleaved/connected scene, and — crucially — forbids
    cross-attribution inside a labeled block.

    The cross-attribution rule is what stops the common failure mode
    where one character's block contains another's spoken dialogue
    or first-person reaction: e.g. `Dr. Priya Anand: *Priya
    steadies the ladder.* "Hold still." Milo wobbles, "I'm trying!"` — the
    inline `Milo` content never gets a `\\n<Name>:` boundary so
    `split_joint_n` leaves it inside Priya's persisted block.
    `app/routes/stream.py:_dispatch_multi_response` also normalizes
    inline `<Other>: "..."` after the fact, but the directive is the
    primary lever; the scrub is a safety net.

    Without this directive at all, the focal-character anchor makes
    the model write a complete single-character beat and stop, so
    multi-response above N=2 needs it to function.
    """
    listing = ", ".join(partner_names)
    n = len(partner_names)
    roster = "\n".join(
        f"  {i}. {nm}:" for i, nm in enumerate([lead_name, *partner_names], start=1)
    )
    return (
        f"[Multi-character scene]\n"
        f"This turn is a multi-character beat. {lead_name} speaks first; "
        f"after them, the other {n} characters present — {listing} — each "
        f"react in turn. All {n + 1} characters in the room speak this turn.\n"
        f"\n"
        f"ROSTER — write exactly these {n + 1} blocks, in this order, one "
        f"block per character, each beginning with its numbered label "
        f"copied from this list:\n"
        f"{roster}\n"
        f"The numbered label line is MANDATORY before every block — the "
        f"number, the name, the colon, on its own line (e.g. "
        f"`2. {partner_names[0] if partner_names else lead_name}:`). A "
        f"block without its label is unusable and gets discarded. These "
        f"are the ONLY characters in the scene; refer to them by these "
        f"names in your prose. The roster is AUTHORITATIVE: every listed "
        f"character IS present in the room right now — never question, "
        f"debate, or reason about who is present, and never write notes "
        f"to yourself about it; just write each character's block. Count "
        f"your blocks before finishing: if any character on the roster "
        f"has not spoken, add their block — even a single line of "
        f"reaction — before you end. Do not stop early.\n"
        f"\n"
        f"CRITICAL — block isolation:\n"
        f"Each labeled block contains ONLY that character's own actions and "
        f"dialogue. Never put another character's spoken dialogue, internal "
        f"thoughts, or first-person reactions inside your block. You may "
        f"OBSERVE others externally — \"Dex's hand tightens on the cover\" "
        f"is fine because your character can see it — but you must not "
        f"speak or quote for them. When another character speaks or reacts, "
        f"END YOUR BLOCK and start theirs on a NEW LINE with their "
        f"numbered label.\n"
        f"\n"
        f"Format inside each labeled block: *asterisks* around actions "
        f"and physical description, \"quotes\" around spoken dialogue. "
        f"This applies to EVERY character's block — the lead and every "
        f"partner — not just the lead. The block-isolation rule above "
        f"governs labels and attribution; this rule governs voice. "
        f"Both apply at the same time.\n"
        f"\n"
        f"Opening rule: the FIRST character of the line immediately "
        f"after a label is `*`. Always. The block always opens with "
        f"an action beat in *asterisks*, then any \"quoted\" dialogue "
        f"can follow inline. Do not start a block with bare prose and "
        f"add a closing `*` partway through — that leaves the opening "
        f"action unwrapped and the format is wrong even if it self-"
        f"corrects later.\n"
        f"\n"
        f"No meta commentary. The output is in-character prose ONLY. "
        f"Never write planning or commentary about the prompt, the "
        f"roster, the instructions, or what you are about to write "
        f"(e.g. \"the prompt didn't specify…\", \"I will include the "
        f"others present…\"). If you catch yourself explaining the task, "
        f"stop and write the scene itself instead. Reasoning never "
        f"belongs in the output — not even wrapped in *asterisks*.\n"
        f"\n"
        f"No markdown. This is prose, not a document. Do NOT use markdown "
        f"headings (`#`), bold or italics (`**`, `__`, `_`), bullet lists, "
        f"blockquotes, or horizontal rules anywhere. The ONLY special "
        f"characters in the output are `*` around actions, `\"` around "
        f"dialogue, and the numbered speaker labels. A label is a bare "
        f"`1. {lead_name}:` at line start — never bolded "
        f"(`**1. {lead_name}:**`), never a heading, never decorated. A "
        f"label wrapped in markdown can break the split and merge two "
        f"characters' lines into one block.\n"
        f"\n"
        f"Bad — labels-only output that drops the *asterisks* / \"quotes\" "
        f"format under multi-character pressure (the format rule from the "
        f"system prompt above still applies):\n"
        f"  1. Dr. Priya Anand:\n"
        f"  Priya crouches over him, palm flat against his shoulder. Hold still.\n"
        f"\n"
        f"Bad — Priya's block voicing Milo inline (Milo's line gets "
        f"lost into Priya's persisted block):\n"
        f"  1. Dr. Priya Anand:\n"
        f"  *Priya crouches.* \"Hold still.\" Milo wobbles, \"I'm trying!\"\n"
        f"\n"
        f"Good — numbered labels on new lines, actions in *asterisks*, "
        f"dialogue in \"quotes\":\n"
        f"  1. Dr. Priya Anand:\n"
        f"  *Priya crouches over him, palm flat against his shoulder.* "
        f"\"Hold still.\"\n"
        f"  2. Milo:\n"
        f"  *He winces under her hand.* \"I'm trying!\"\n"
        f"\n"
        f"Beats should reference each other — interrupt, agree, build on "
        f"the previous beat — but voicing happens only inside the speaker's "
        f"own labeled block. Voice each character distinctly; do not borrow "
        f"another character's mannerisms, accent, or vocabulary. Do not "
        f"narrate outside character beats."
    )


def build_partner_voice_blocks(
    cid: str,
    partner_ids: list[str],
    *,
    pairs_per_partner: int = 3,
    mannerisms_per_partner: int = 4,
    pair_char_cap: int = 400,
    conversation: dict[str, Any] | None = None,
    leaf_id: str | None = None,
) -> str:
    """Per-partner voice priming for the lead's multi-character turn.

    Without this, the multi-response prompt only carries the lead's full
    character card; partners surface only through ``_others_present_text``
    (name + outfit + body description). The model has no voice priming
    for them, and the partners come out sounding like the lead — the
    "characters not kept separate" failure mode.

    For each partner we emit a compact block with the partner's canonical
    label, the first sentence of their description, the first N
    mannerisms (concrete embodied tells), and the first N dialogue_pairs
    rendered as labelled lines so the model has a sample of how that
    character actually talks and reacts. Sized so a five-partner scene
    stays around ~7.5 KB of voice priming — denser than the original
    ~3 KB at 1 pair × 260 chars, after partners visibly dropped
    *asterisk* / "quote" formatting when their format demonstration
    was a single truncated sample compared to the lead's 22-pair
    primer in the messages array.

    `pair_char_cap` truncates each pair's char body to keep the budget
    predictable; the lead's full dialogue_pairs still go through as
    primer turns in the messages array (unaffected).

    When ``conversation`` and ``leaf_id`` are both provided, the partner
    card is rendered with the partner's CURRENT effective outfit
    (path-replayed from the active leaf) injected as a "Currently in
    this scene:" line near the top of the block. This anchors the
    voice priming — the dialogue_pairs often reference baseline state
    (school uniform shirt, etc.) and a fresh narrator outfit edit
    otherwise gets overpowered by ~1.2 KB of uniform-coded prose per
    partner. The lead's [Others present] block carries the new outfit
    too, but it's a single mention vs the voice block's much denser
    state cue. Falls back to baseline ``load_instance_entities`` (no
    current-outfit injection) when neither is given.

    Returns the concatenated blocks separated by blank lines, or empty
    string when no partner has any usable voice data.
    """
    if not partner_ids:
        return ""
    # Effective state (path-replayed) — used to inject each partner's
    # current outfit / state into the voice block. Falls back to
    # baseline when the caller didn't supply a conversation + leaf so
    # standalone callers keep working.
    eff_ents: dict[str, Any] = {}
    if conversation is not None and leaf_id:
        try:
            from .effective import effective_entities_at as _eff
            eff_ents = _eff(conversation, leaf_id) or {}
        except Exception:
            eff_ents = {}
    try:
        ents = load_instance_entities(cid) or {}
    except Exception:
        ents = {}

    blocks: list[str] = []
    for pid in partner_ids:
        # Prefer the effective (path-replayed) entity when available
        # — that's what the lead's prompt was assembled from, and what
        # the model needs to see as the partner's current state.
        ent = eff_ents.get(pid) if eff_ents else None
        if not isinstance(ent, dict):
            ent = ents.get(pid)
        # Fall back to the master template catalog when the instance
        # dir doesn't have this partner yet — shouldn't normally happen
        # since the multi-response cast filter already verified branch
        # membership, but defensive.
        if not isinstance(ent, dict):
            try:
                from .entities import get as _ent_get
                ent = _ent_get(pid)
            except Exception:
                ent = None
        if not isinstance(ent, dict):
            continue

        name = ent.get("name") or pid
        props = ent.get("properties") or {}

        parts: list[str] = [f"[Voicing — {name}]"]

        # Current-scene anchor — outfit + transient status notes. Lands
        # FIRST after the header so the dialogue_pairs that follow are
        # read against the current state, not the baseline they were
        # authored under. Only emitted when we have effective state.
        if eff_ents:
            state_lines: list[str] = []
            outfit_id = props.get("current_outfit")
            outfit_ent = eff_ents.get(outfit_id) if outfit_id else None
            if not isinstance(outfit_ent, dict):
                outfit_ent = ents.get(outfit_id) if outfit_id else None
            if isinstance(outfit_ent, dict):
                op = outfit_ent.get("properties") or {}
                # Prefer the concise one-liner; fall back to intact desc,
                # then to the outfit name, then to the bare id.
                outfit_text = (
                    op.get("concise_description")
                    or op.get("intact_description")
                    or outfit_ent.get("name")
                    or outfit_id
                )
                if outfit_text:
                    state_lines.append(f"Currently wearing: {outfit_text}")
            elif outfit_id:
                state_lines.append(f"Currently wearing: {outfit_id}")
            notes = props.get("notes") or {}
            if isinstance(notes, dict):
                status = notes.get("status")
                if isinstance(status, str) and status.strip():
                    state_lines.append(f"Current state: {status.strip()}")
            if state_lines:
                parts.append("Currently in this scene:")
                parts.extend(f"- {line}" for line in state_lines)

        # Per-character content guardrail. In single-character mode this is
        # surfaced as a hard "Content boundary (MUST follow)" line
        # (personas.py); in multi mode a partner is voiced inside the lead's
        # turn, so without this the partner's boundary is silently dropped and
        # unenforced. Emit it prominently for each partner that declares one.
        boundaries = props.get("boundaries")
        if isinstance(boundaries, str) and boundaries.strip():
            parts.append(f"Content boundary (MUST follow): {boundaries.strip()}")

        # First sentence of description seeds identity / voice register
        # without dragging the whole bio into prompt space.
        desc = ent.get("description") or ""
        if isinstance(desc, str) and desc.strip():
            first = desc.strip().split(". ")[0].strip()
            if first:
                if not first.endswith(("."  , "!", "?")):
                    first += "."
                parts.append(first)

        # Concrete behavioural tells — the highest-leverage non-dialogue
        # source per docs/prompt_anatomy.md.
        mannerisms = props.get("mannerisms")
        if isinstance(mannerisms, list):
            sample = [m for m in mannerisms[:mannerisms_per_partner] if isinstance(m, str) and m.strip()]
            if sample:
                parts.append("Mannerisms:")
                parts.extend(f"- {m}" for m in sample)

        # Signature clothing-meets-body tells + a scent anchor — cheap, high-
        # leverage voice anchors that single-character mode includes and multi
        # mode previously omitted entirely for partners.
        tells = props.get("signature_physical_tells")
        if isinstance(tells, list):
            tell_sample = [t for t in tells[:mannerisms_per_partner] if isinstance(t, str) and t.strip()]
            if tell_sample:
                parts.append("Physical tells:")
                parts.extend(f"- {t}" for t in tell_sample)
        scent = props.get("scent")
        if isinstance(scent, dict):
            general = scent.get("general")
            if isinstance(general, str) and general.strip():
                parts.append(f"Scent: {general.strip()}")

        # Dialogue pairs rendered as labelled lines matching the
        # multi-character directive's "label on its own line, content
        # on the next" shape. Using `<Name>: <content>` on a single
        # line (the previous shape) gave the model TWO different
        # patterns for what comes right after a label — the directive's
        # newline-then-asterisk vs the voice-block's space-then-asterisk
        # — and the model resolved that by following the directive's
        # newline but losing the opening `*` in the transition. Putting
        # both at "label\n*action* \"dialogue\"" aligns them.
        pairs = props.get("dialogue_pairs")
        if isinstance(pairs, list):
            voice_lines: list[str] = []
            for p in pairs[:pairs_per_partner]:
                if not isinstance(p, dict):
                    continue
                u = (p.get("user") or "").strip()
                c = (p.get("char") or p.get("response") or "").strip()
                if not (u and c):
                    continue
                if len(c) > pair_char_cap:
                    c = c[: pair_char_cap].rstrip() + "…"
                voice_lines.append(f"User:\n{u}")
                voice_lines.append(f"{name}:\n{c}")
                # Blank line between pairs so the model reads them as
                # separate exchanges, not a continuous block.
                voice_lines.append("")
            if voice_lines and voice_lines[-1] == "":
                voice_lines.pop()
            if voice_lines:
                parts.append("Voice (how this character speaks):")
                parts.extend(voice_lines)

        # Only emit a block when there's something useful beyond the
        # header — otherwise we'd be telling the model "voice X" with
        # no priming, which is worse than not telling them anything.
        if len(parts) > 1:
            blocks.append("\n".join(parts))

    return "\n\n".join(blocks)


def display_name_of(cid: str, char_id: str) -> str:
    """Return the character's canonical display name (the JSON `name`
    field). For label-matching across aliases, use ``display_labels_of``
    instead — this returns just the canonical."""
    try:
        ents = load_instance_entities(cid)
        ent = ents.get(char_id) or {}
        return ent.get("name") or char_id
    except Exception:
        return char_id


def display_names(cid: str, char_ids: list[str]) -> list[str]:
    return [display_name_of(cid, c) for c in char_ids]


def display_labels_of(cid: str, char_id: str) -> list[str]:
    """Return every label the multi-response split should accept for
    this character — canonical name + properties.aliases + auto-
    derived title-stripped variants. The first element is always the
    canonical (`display_name_of`), which the directive uses as the
    primary label the model is asked to produce.
    """
    try:
        ents = load_instance_entities(cid)
        ent = ents.get(char_id) or {}
    except Exception:
        ent = {}
    labels = aliases_for(ent)
    return labels or [char_id]


# ---------------------------------------------------------------------------
# Prompt-registry blocks
#
# Multi-character mode appends two things to the lead's (character)
# system prompt: per-partner voice priming, then the joint directive.
# These are registered as character-persona blocks at orders 250/260 so
# they compose AFTER style_discipline (190) via the same assembler as
# every other prompt — single source of truth, introspectable through
# `list_blocks`. They fire only when the caller supplies multi data via
# `ctx.settings["_multi"]` (populated by the stream route, which already
# resolves partners for the stop-list handling). When multi isn't active
# the blocks return None and the prompt is unchanged.
#
# `_multi` shape: {"lead_name": str, "partner_ids": [str],
#                  "partner_names": [str]}.
# ---------------------------------------------------------------------------
from .prompt import Block as _Block, register as _register  # noqa: E402


@_register(id="multi_partner_voice", order=250, applies_to=("character",))
def _block_multi_partner_voice(ctx):
    multi = ctx.settings.get("_multi") or {}
    partner_ids = multi.get("partner_ids") or []
    if not partner_ids:
        return None
    cid = (ctx.conversation or {}).get("id")
    if not cid:
        return None
    text = build_partner_voice_blocks(
        cid, partner_ids, conversation=ctx.conversation, leaf_id=ctx.leaf_id,
    )
    if not text:
        return None
    return _Block(label="Multi-character partner voices", content=text, section=None)


@_register(id="multi_joint_directive", order=260, applies_to=("character",))
def _block_multi_joint_directive(ctx):
    multi = ctx.settings.get("_multi") or {}
    partner_names = multi.get("partner_names") or []
    lead_name = multi.get("lead_name")
    if not partner_names or not lead_name:
        return None
    return _Block(
        label="Multi-character directive",
        content=build_joint_directive(lead_name, partner_names),
        section=None,
    )
