from datetime import datetime

import pytest

from app import db, seed
from app.config import IST

# A Monday morning before any clinic slot has started.
MONDAY_8AM = datetime(2026, 11, 16, 8, 0, tzinfo=IST)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    seed.seed(c, now=MONDAY_8AM)
    yield c
    c.close()
