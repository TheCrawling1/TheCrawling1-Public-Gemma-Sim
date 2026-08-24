"""Login, logout, and first-run setup."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from ..auth import (
    create_user,
    has_any_user,
    login_session,
    logout_session,
    verify_user,
)


bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not has_any_user():
        return redirect(url_for("auth.setup"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if verify_user(username, password):
            login_session(username)
            nxt = request.args.get("next") or url_for("pages.dashboard")
            return redirect(nxt)
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if has_any_user():
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        try:
            if password != confirm:
                raise ValueError("Passwords do not match.")
            create_user(username, password)
            login_session(username)
            return redirect(url_for("pages.dashboard"))
        except ValueError as e:
            flash(str(e), "error")
    return render_template("setup.html")


@bp.route("/logout", methods=["POST", "GET"])
def logout():
    logout_session()
    return redirect(url_for("auth.login"))
