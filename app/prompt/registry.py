"""Block registry + assembler.

A registered block is a function `(PromptContext) -> Block | None`.
The assembler iterates blocks in order, collects the non-None ones,
and emits BOTH the system_text AND the dev-panel pieces from the
same list. They can't drift.

Ordering uses integer slots with 10-wide gaps for cheap insertion
between blocks:

    10-99     core blocks  (this package's `core.py`)
    100-199   module blocks  (e.g. app/life_sim.py)
    200-249   prefab blocks
    250-299   trailing core (style discipline)

This is a convention, not enforcement — duplicate order numbers are
fine and tie-break by id. Modules and prefabs pick numbers in their
range so the core can't accidentally land on top of a module block.

Persona routing is via `applies_to=("character", "narrator", ...)`.
A block is included in the assembly iff `ctx.persona in applies_to`.

Re-registering the same id replaces the earlier entry — useful for
tests / debugging, not intended for production overrides.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .context import PromptContext


# ---------------------------------------------------------------------------
# Block — the unit of output
# ---------------------------------------------------------------------------


@dataclass
class Block:
    """The rendered output of a registered block.

    `content` is the body text. `section` is the bracketed header
    that wraps it in the system prompt (e.g. "Surroundings" →
    `[Surroundings]\\n<content>`); `section=None` renders bare
    (the `system_prompt` block uses this).

    `label` is the dev-panel display name; usually matches `section`
    but doesn't have to.

    `dev_panel_only=True` keeps a block out of the system text and
    surfaces it only in pieces — used for things like
    "Truncation: N messages elided" notices.

    `tags` are declarative purpose tags carried through from the
    `@register(..., tags=("environmental",))` decorator. Modules
    register filters that gate blocks by tag — e.g. the texting
    module drops blocks tagged ``environmental`` when the focal is
    replying to a texted message. Engine blocks never check tags
    themselves; the filter dispatch in `assemble()` is the only
    consumer.
    """
    label: str
    content: str
    section: str | None = None
    dev_panel_only: bool = False
    tags: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Registry — a single module-level table of registered entries
# ---------------------------------------------------------------------------


_RegistryFn = Callable[[PromptContext], Optional[Block]]


@dataclass
class _Entry:
    id: str
    order: int
    applies_to: tuple[str, ...]
    tags: tuple[str, ...]
    fn: _RegistryFn


_REGISTRY: dict[str, _Entry] = {}


def register(
    *,
    id: str,
    order: int,
    applies_to: tuple[str, ...] = ("character", "narrator"),
    tags: tuple[str, ...] = (),
) -> Callable[[_RegistryFn], _RegistryFn]:
    """Decorator: register `fn` as a prompt block.

    `id` is unique. Re-registering an id replaces the prior entry.
    `order` is the integer slot (see module docstring for ranges).
    `applies_to` filters by `ctx.persona` — defaults to both.
    `tags` are declarative purpose tags. Modules register filters
    (via `register_filter`) that gate blocks by tag — e.g. the
    texting module drops blocks tagged ``environmental``.

    The tag propagates to the returned `Block` so module filters
    can inspect either the entry (for id/tags) or the block (for
    label/content). Filters get both.

    Example:

        @register(id="surroundings", order=60,
                  applies_to=("character",),
                  tags=("environmental",))
        def block_surroundings(ctx):
            text = _surroundings_text(ctx.entities, ctx.presence, ctx.macros)
            if not text:
                return None
            return Block(label="Surroundings", content=text, section="Surroundings")
    """
    def wrap(fn: _RegistryFn) -> _RegistryFn:
        _REGISTRY[id] = _Entry(
            id=id, order=order, applies_to=applies_to, tags=tags, fn=fn,
        )
        return fn
    return wrap


# Module-contributed filters. Each is a callable
# `(entry, block, ctx) -> bool`. Returning False drops the block from
# the assembled prompt. All filters are AND'd together — any one
# returning False is enough to drop. Filters fire in registration
# order; module load order is the discovery order
# (`app/modules/loader.py`).
_FILTERS: list[Callable[[_Entry, "Block", PromptContext], bool]] = []


def register_filter(
    fn: Callable[[_Entry, "Block", PromptContext], bool],
) -> Callable[[_Entry, "Block", PromptContext], bool]:
    """Register a module filter. The filter gets the block's registry
    entry (id, tags, order), the rendered Block, and the PromptContext;
    returns False to drop the block before it lands in system_text or
    pieces.

    Used by modules to gate engine blocks by tag without coupling the
    engine block functions to specific modules. The texting module's
    environmental-strip filter is the canonical example.
    """
    _FILTERS.append(fn)
    return fn


def clear_filters() -> None:
    """Test-only helper. Drops all registered filters."""
    _FILTERS.clear()


# Module-contributed message annotators. Each is a callable
# `(conversation, message) -> dict | None`. After a message node is
# persisted, the engine runs every annotator and deep-merges the
# returned dicts into `message["metadata"]`. This lets a module record
# branch-specific facts ABOUT the message on the node itself — the same
# way narrator edits land in `metadata.applied_edits` — so the fact
# survives a reload without the client having to re-derive it.
#
# The canonical use is the texting module: it annotates a character
# reply with `{"modules": {"texting": {...}}}` when the reply is
# answering a text, so the SMS render style re-applies on reload
# instead of depending on a fragile client-side parent walk.
_MESSAGE_ANNOTATORS: list[Callable[[dict, dict], "dict | None"]] = []


def register_message_annotator(
    fn: Callable[[dict, dict], "dict | None"],
) -> Callable[[dict, dict], "dict | None"]:
    """Register a `(conversation, message) -> dict | None` annotator.

    Called once per freshly-persisted message; whatever dict it returns
    is deep-merged into the message's `metadata`. Return None to skip.
    Keeps per-message persistence decoupled from specific modules the
    same way `register_filter` keeps block-gating decoupled.
    """
    _MESSAGE_ANNOTATORS.append(fn)
    return fn


def clear_message_annotators() -> None:
    """Test-only helper. Drops all registered annotators."""
    _MESSAGE_ANNOTATORS.clear()


def _deep_merge(dst: dict, src: dict) -> dict:
    """Recursively merge `src` into `dst` (dicts merge, other values
    overwrite). Mutates and returns `dst`."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def run_message_annotators(conversation: dict, message: dict) -> dict:
    """Run every registered annotator against `message` and deep-merge
    the results into `message["metadata"]`. Returns the message for
    convenience. Annotator errors are swallowed so one bad module can't
    break message persistence."""
    for fn in _MESSAGE_ANNOTATORS:
        try:
            extra = fn(conversation, message)
        except Exception:
            continue
        if isinstance(extra, dict) and extra:
            meta = message.setdefault("metadata", {})
            if isinstance(meta, dict):
                _deep_merge(meta, extra)
    return message


_OUTPUT_FILTERS: list[Callable[[dict, str], str]] = []


def register_output_filter(
    fn: Callable[[dict, str], str],
) -> Callable[[dict, str], str]:
    """Register a `(conversation, text) -> text` filter applied to a
    freshly-generated character/narrator message's TEXT before it is
    persisted. Lets a module scrub or rewrite model output — e.g. a
    module that injects mechanical instructions the model might echo can
    strip the echo — without the engine embedding any module-specific
    knowledge. Filters run in registration order; each receives the
    previous filter's output. A filter should cheap-exit (return `text`
    unchanged) when it isn't relevant to the conversation."""
    _OUTPUT_FILTERS.append(fn)
    return fn


def clear_output_filters() -> None:
    """Test-only helper. Drops all registered output filters."""
    _OUTPUT_FILTERS.clear()


def run_output_filters(conversation: dict, text: str) -> str:
    """Apply every registered output filter to `text`, in order. A filter
    that raises (or returns a non-str) is skipped so one bad module can't
    corrupt the response. Returns the filtered text."""
    for fn in _OUTPUT_FILTERS:
        try:
            out = fn(conversation, text)
        except Exception:
            continue
        if isinstance(out, str):
            text = out
    return text


def list_blocks(persona: str | None = None) -> list[_Entry]:
    """Return registered blocks in render order. Filter by persona
    when provided. Used by the assembler and by future per-conv
    block-customization UI."""
    entries = sorted(_REGISTRY.values(), key=lambda e: (e.order, e.id))
    if persona is None:
        return entries
    return [e for e in entries if persona in e.applies_to]


def unregister(id: str) -> None:
    """Remove a block from the registry. Test-only helper."""
    _REGISTRY.pop(id, None)


# ---------------------------------------------------------------------------
# Assembler — runs the registry, emits system_text + pieces
# ---------------------------------------------------------------------------


@dataclass
class AssembledPrompt:
    """Output of `assemble()`. `system` is the joined prompt text the
    model receives in the system slot. `pieces` is the dev-panel list
    of `{label, content}` dicts the existing UI consumes. `blocks` is
    the raw Block list for debugging / introspection."""
    system: str
    pieces: list[dict[str, str]]
    blocks: list[Block]


def assemble(ctx: PromptContext) -> AssembledPrompt:
    """Run every registered block applicable to this persona, in
    order, and emit system_text + pieces from the same Block list.

    Blocks that return None are skipped. After a block renders, every
    registered module filter is run; any filter returning False drops
    the block. `dev_panel_only` blocks contribute to `pieces` but not
    to `system`.

    This function is the entire assembler — every behaviour
    difference between persona kinds lives in the blocks themselves
    (via `applies_to` / by returning None) or in module-contributed
    filters (via `register_filter`).
    """
    blocks: list[Block] = []
    for entry in list_blocks(ctx.persona):
        block = entry.fn(ctx)
        if block is None:
            continue
        # Propagate entry tags onto the block so any downstream
        # consumer (filters, debug introspection, future per-block
        # UIs) can read them off the Block alone.
        if entry.tags and not block.tags:
            block.tags = entry.tags
        # Module filter dispatch — any False drops the block.
        if _FILTERS and not all(f(entry, block, ctx) for f in _FILTERS):
            continue
        blocks.append(block)

    system_parts: list[str] = []
    for b in blocks:
        if b.dev_panel_only:
            continue
        if b.section:
            system_parts.append(f"[{b.section}]\n{b.content}")
        else:
            system_parts.append(b.content)
    system_text = "\n\n".join(system_parts)

    pieces = [{"label": b.label, "content": b.content} for b in blocks]
    return AssembledPrompt(system=system_text, pieces=pieces, blocks=blocks)
