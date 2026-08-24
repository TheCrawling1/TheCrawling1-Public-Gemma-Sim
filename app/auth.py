"""Username + password auth, server-side sessions.

Single user today, but every record is keyed by username so multi-user is a
non-breaking addition later.
"""
from __future__ import annotations

import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import current_app, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .storage import read_json, write_json


def _users_path() -> Path:
    return Path(current_app.config["data_dir"]) / "users.json"


def _load_users() -> dict[str, Any]:
    return read_json(_users_path(), default={"users": {}}) or {"users": {}}


def has_any_user() -> bool:
    return bool(_load_users().get("users"))


def create_user(username: str, password: str) -> None:
    if not username or not username.strip():
        raise ValueError("Username required.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    data = _load_users()
    users = data.setdefault("users", {})
    uname = username.strip().lower()
    if uname in users:
        raise ValueError("User already exists.")
    users[uname] = {
        "password_hash": generate_password_hash(password, method="scrypt"),
        "created_at": int(time.time()),
    }
    write_json(_users_path(), data)


def verify_user(username: str, password: str) -> bool:
    users = _load_users().get("users", {})
    record = users.get((username or "").strip().lower())
    if not record:
        return False
    return check_password_hash(record["password_hash"], password)


def login_session(username: str) -> None:
    session.clear()
    session["user"] = (username or "").strip().lower()
    session["logged_in_at"] = int(time.time())


def logout_session() -> None:
    session.clear()


def current_user() -> str | None:
    return session.get("user")


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            if not has_any_user():
                return redirect(url_for("auth.setup"))
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped
