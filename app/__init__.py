import os
import secrets

from flask import Flask, send_from_directory

from .db import init_db


def create_app(config=None):
    app = Flask(__name__, static_folder=os.path.join(os.path.dirname(__file__), "..", "static"), static_url_path="")
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "..", "ops.db")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        START_WORKER=True,
        SEED=True,
    )
    if config:
        app.config.update(config)
    init_db(app.config["DATABASE"])
    if app.config["SEED"]:
        from .seed import seed
        seed(app.config["DATABASE"])

    from .api import api
    app.register_blueprint(api)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    if app.config["START_WORKER"]:
        from .worker import start
        start(app.config["DATABASE"])
    return app
