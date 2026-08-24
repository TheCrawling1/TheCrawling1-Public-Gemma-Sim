"""Registry-based prompt assembler.

Public API. Submodules:

  - context:   PromptContext dataclass + build_context()
  - registry:  Block dataclass + @register decorator + assemble()
  - core:      core blocks shipped by the engine

Modules and prefabs that want to contribute prompt blocks import
`register` + `Block` + `PromptContext` from here and decorate their
own functions. The engine never reaches into module code; modules
self-register at import time.

See docs/prompt_anatomy.md (TODO) for the full block reference.
"""
from .context import PromptContext, build_context
from .registry import (
    Block,
    assemble,
    list_blocks,
    register,
    register_filter,
    register_message_annotator,
    register_output_filter,
    run_message_annotators,
    run_output_filters,
)

# Import the core blocks for their side effects (registration).
from . import core  # noqa: F401

# Modules under data/modules/<id>/ self-register their prompt blocks
# at engine startup via app.modules.loader.load_all_module_code()
# (called from create_app). Nothing in this package needs to know
# which modules exist — the loader imports each <id>.py file and
# their @register decorators run as side effects.

__all__ = [
    "Block",
    "PromptContext",
    "assemble",
    "build_context",
    "list_blocks",
    "register",
    "register_filter",
    "register_message_annotator",
    "register_output_filter",
    "run_message_annotators",
    "run_output_filters",
]
