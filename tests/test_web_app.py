"""App factory smoke tests."""

from pathlib import Path

from ciw.config import Config
from ciw.web import create_app


def test_create_app_smoke(client):
    assert client.get("/").status_code == 200
    rules = {r.rule for r in client.application.url_map.iter_rules()}
    assert {"/", "/browse", "/post/<int:post_id>", "/search", "/primers"} <= rules


def test_create_app_uses_the_configured_blog_db(tmp_path):
    cfg = Config(root=tmp_path, blog_db=Path("/nonexistent/blog.db"))
    app = create_app(cfg)
    assert app.config["BLOG_DB"] == "/nonexistent/blog.db"
    assert app.config["CIW"] is cfg


def test_each_call_returns_an_independent_app():
    assert create_app() is not create_app()


def test_nav_active_class_follows_the_blueprint_endpoints(client):
    def active(path):
        html = client.get(path).get_data(as_text=True)
        return [
            label
            for label in ("Home", "Primers", "All Posts")
            if f'class="active">{label}</a>' in html
        ]

    assert active("/") == ["Home"]
    assert active("/primers") == ["Primers"]
    assert active("/browse") == ["All Posts"]
    assert active("/search?q=agar") == []
