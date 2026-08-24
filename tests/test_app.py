"""App factory + auth gating (no Ollama required)."""
import pytest

from app import create_app
from app.merge import deep_merge


@pytest.fixture(scope="module")
def app():
    return create_app()


def test_create_app_boots(app):
    assert app is not None
    # core blueprints are registered
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/" in rules
    assert any(r.startswith("/chat/") for r in rules)
    assert "/studio" in rules


def test_dashboard_requires_login(app):
    client = app.test_client()
    resp = client.get("/")
    # login_required redirects unauthenticated users away from the dashboard
    assert resp.status_code in (301, 302)
    assert "/login" in resp.headers.get("Location", "") or "/setup" in resp.headers.get(
        "Location", ""
    )


def test_first_run_setup_page_renders(app):
    client = app.test_client()
    resp = client.get("/setup")
    assert resp.status_code == 200


def test_deep_merge_nested():
    base = {"a": 1, "nested": {"x": 1, "y": 2}}
    deep_merge(base, {"nested": {"y": 20, "z": 30}, "b": 2})
    assert base == {"a": 1, "b": 2, "nested": {"x": 1, "y": 20, "z": 30}}
