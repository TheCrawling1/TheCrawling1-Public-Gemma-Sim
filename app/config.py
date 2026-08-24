"""Configuration loading. config.local.json overrides config.example.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import current_app


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = PROJECT_ROOT / "config.example.json"
LOCAL_PATH = PROJECT_ROOT / "config.local.json"


def load_config() -> dict[str, Any]:
    base: dict[str, Any] = {}
    if EXAMPLE_PATH.exists():
        base = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    if LOCAL_PATH.exists():
        local = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
        base = _deep_merge(base, local)

    # Resolve relative paths against the project root.
    for key in ("data_dir", "instances_dir", "session_dir"):
        value = base.get(key)
        if value and not Path(value).is_absolute():
            base[key] = str(PROJECT_ROOT / value)

    # Normalize keys for Flask (uppercase top-level).
    return {
        **base,
        "HOST": base.get("host", "127.0.0.1"),
        "PORT": base.get("port", 5000),
        "DEBUG": base.get("debug", False),
    }


def save_local_overrides(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge `updates` into config.local.json and reload the running app.

    Returns the freshly loaded config so callers can echo it back.
    """
    from .storage import write_json  # local import to avoid circular at module load

    existing: dict[str, Any] = {}
    if LOCAL_PATH.exists():
        existing = json.loads(LOCAL_PATH.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(existing, updates)
    write_json(LOCAL_PATH, merged)

    fresh = load_config()
    try:
        current_app.config.update(fresh)
    except RuntimeError:
        pass  # No app context (eg. during tests)
    return fresh


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
