"""Flask web app: app factory and blueprints."""

from __future__ import annotations

from flask import Flask

from ciw.config import Config, load_config


def create_app(config: Config | None = None) -> Flask:
    """Build the Flask app. ``config`` defaults to :func:`ciw.config.load_config`."""
    from ciw.web import blog, episodes

    cfg = config if config is not None else load_config()
    app = Flask(__name__)
    app.config["CIW"] = cfg
    app.config["BLOG_DB"] = str(cfg.blog_db)

    app.add_template_filter(blog.fmt_date, "fmt_date")
    app.add_template_filter(blog.short_excerpt, "short_excerpt")
    app.register_blueprint(blog.bp)
    app.register_blueprint(episodes.bp)
    app.add_template_filter(episodes.clock, "clock")
    return app
