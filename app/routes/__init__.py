"""Blueprint registration."""
from __future__ import annotations

from flask import Flask

from .auth_routes import bp as auth_bp
from .pages import bp as pages_bp
from .api import bp as api_bp
from .modules import bp as modules_bp
from .prefabs import bp as prefabs_bp
from .sprites import bp as sprites_bp
from .stream import bp as stream_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(auth_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(stream_bp, url_prefix="/api")
    app.register_blueprint(sprites_bp, url_prefix="/sprites")
    app.register_blueprint(modules_bp)
    app.register_blueprint(prefabs_bp)
