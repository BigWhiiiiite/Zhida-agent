"""Isolated V2 integration fixtures: never infer URLs from backend/.env."""
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.backend_v2.db import Database


@pytest.fixture(scope="session")
def v2_db():
    url = os.getenv("ZHIDA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("real PostgreSQL requires explicit ZHIDA_TEST_DATABASE_URL")
    parsed = make_url(url)
    if not (parsed.database or "").startswith("zhida_test"):
        pytest.fail("Integration tests only accept a database named zhida_test*")
    previous = os.environ.get("ZHIDA_V2_DATABASE_URL")
    os.environ["ZHIDA_V2_DATABASE_URL"] = url
    from app.backend_v2.settings import settings
    settings.cache_clear()
    try:
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic-v2.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("ZHIDA_V2_DATABASE_URL", None)
        else:
            os.environ["ZHIDA_V2_DATABASE_URL"] = previous
        settings.cache_clear()
    db = Database(url)
    yield db
    db.close()
