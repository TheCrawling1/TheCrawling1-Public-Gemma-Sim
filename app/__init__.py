"""Flask app factory."""
from __future__ import annotations

import ipaddress
import logging
import secrets
from pathlib import Path

from flask import Flask, abort, current_app, request
from flask_session import Session

from .config import load_config


log = logging.getLogger(__name__)


# Loopback addresses are always allowed regardless of the configured
# allowlist — locking yourself out of your own machine via the UI would
# be a footgun.
_LOOPBACK = {"127.0.0.1", "::1"}


def _candidate_ip() -> str | None:
    """Pick the IP to test against the allowlist.

    With `network.trust_proxy` on, honour the first hop of the
    X-Forwarded-For chain (the original client behind a reverse proxy).
    Otherwise use Werkzeug's request.remote_addr (the direct peer).
    """
    cfg = current_app.config.get("network") or {}
    if cfg.get("trust_proxy"):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                return first
    return request.remote_addr


def _ip_allowed(ip: str | None, allowlist: list[str]) -> bool:
    """Exact-match IPv4/IPv6 plus CIDR ranges via 'a.b.c.d/N'."""
    if not ip:
        return False
    if ip in _LOOPBACK:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowlist:
        entry = (entry or "").strip()
        if not entry:
            continue
        if "/" in entry:
            try:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        else:
            if entry == ip:
                return True
    return False


def _enforce_ip_allowlist():
    """before_request hook: 403 anything not on the allowlist.

    Empty allowlist = open mode (the default in config.example.json so
    the app keeps working out of the box on localhost). Add your LAN
    IPs in config.local.json under `network.allowed_ips` — the UI on
    the Settings page does this for you.
    """
    cfg = current_app.config.get("network") or {}
    allowlist = list(cfg.get("allowed_ips") or [])
    if not allowlist:
        return  # open mode
    ip = _candidate_ip()
    if _ip_allowed(ip, allowlist):
        return
    log.warning("blocked request from %s (path=%s)", ip, request.path)
    abort(403)


def create_app() -> Flask:
    cfg = load_config()
    logging.basicConfig(
        level=logging.DEBUG if cfg.get("DEBUG") else logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config.update(cfg)

    # Secret key: persisted in instance dir so sessions survive restarts.
    instance_dir = Path(app.instance_path)
    instance_dir.mkdir(parents=True, exist_ok=True)
    secret_path = instance_dir / "secret_key"
    if secret_path.exists():
        app.secret_key = secret_path.read_bytes()
    else:
        key = secrets.token_bytes(64)
        secret_path.write_bytes(key)
        secret_path.chmod(0o600)
        app.secret_key = key

    # Server-side sessions on the filesystem.
    session_dir = Path(cfg["session_dir"])
    session_dir.mkdir(parents=True, exist_ok=True)
    app.config["SESSION_TYPE"] = "filesystem"
    app.config["SESSION_FILE_DIR"] = str(session_dir)
    app.config["SESSION_PERMANENT"] = False
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    Session(app)

    # Ensure data directories exist.
    data_dir = Path(cfg["data_dir"])
    for sub in ("characters", "locations", "objects", "scenarios"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    Path(cfg["instances_dir"]).mkdir(parents=True, exist_ok=True)

    from .routes import register_blueprints

    register_blueprints(app)

    # Import every module's backend Python file (`data/modules/<id>/
    # <id>.py`). Module code runs at import time: prompt-block
    # registrations, filter registrations, blueprint mounts all fire
    # here. Errors in a single module log a warning but never raise —
    # one broken module shouldn't take down the engine.
    #
    # Must run inside an app context because module discovery reads
    # `current_app.config["data_dir"]`. Wrapped in app_context() so
    # the module Python files can themselves call Flask helpers
    # during init if they want.
    with app.app_context():
        from .modules.loader import load_all_module_code
        load_all_module_code()
        # Prefabs: importing the package registers the builtin staging
        # kinds; the loader imports any data/prefabs/<id>/prefab.py
        # drop-ins so their custom kinds register too.
        from .prefabs.loader import load_all_prefab_code
        load_all_prefab_code()

    # Cache-bust static assets by file mtime so browsers (mobile Safari/
    # Chrome especially) reliably pick up CSS/JS changes instead of serving
    # a stale cached copy. Templates use `{{ asset('css/style.css') }}`.
    @app.context_processor
    def _asset_helper():
        from flask import url_for
        static_dir = Path(app.static_folder or "")

        def asset(filename: str) -> str:
            try:
                version = int((static_dir / filename).stat().st_mtime)
            except OSError:
                version = 0
            return url_for("static", filename=filename, v=version)

        return {"asset": asset}

    # IP allowlist enforcement runs on every request — registered last
    # so it covers all blueprints. No-op when network.allowed_ips is
    # empty (the default).
    app.before_request(_enforce_ip_allowlist)

    return app
