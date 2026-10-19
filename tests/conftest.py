import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app  # noqa: E402
from app.db import connect  # noqa: E402


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE": str(tmp_path / "test.db"), "SECRET_KEY": "test", "START_WORKER": False})


@pytest.fixture
def db(app):
    conn = connect(app.config["DATABASE"])
    yield conn
    conn.close()


@pytest.fixture
def login(app):
    def make(email):
        client = app.test_client()
        r = client.post("/api/login", json={"email": email, "password": "demo1234"})
        assert r.status_code == 200, r.json
        return client
    return make


def team_id(db, name):
    return db.execute("SELECT id FROM teams WHERE name = ?", (name,)).fetchone()[0]


def item_id(db, title_part):
    return db.execute("SELECT id FROM work_items WHERE title LIKE ?", (f"%{title_part}%",)).fetchone()[0]
