"""JSON file storage with atomic writes.

All persistence flows through here so we have a single chokepoint for safety,
locking, and (eventually) migrations.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _path_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _locks_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def read_json(path: Path | str, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with _path_lock(p):
        return json.loads(p.read_text(encoding="utf-8"))


def write_json(path: Path | str, data: Any) -> None:
    """Atomic write: temp file + fsync + rename in the same directory."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _path_lock(p):
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=p.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def list_json_files(directory: Path | str) -> list[Path]:
    d = Path(directory)
    if not d.exists():
        return []
    return sorted(p for p in d.rglob("*.json") if p.is_file())


def delete_path(path: Path | str) -> None:
    p = Path(path)
    if p.is_file():
        p.unlink(missing_ok=True)
    elif p.is_dir():
        import shutil
        shutil.rmtree(p, ignore_errors=True)
