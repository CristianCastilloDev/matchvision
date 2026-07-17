from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


_TEST_ROOT = Path(tempfile.mkdtemp(prefix="matchvision-tests-"))
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_ROOT / 'test.db'}"
os.environ["DATA_ROOT"] = str(_TEST_ROOT / "data")
os.environ["MODEL_ROOT"] = str(_TEST_ROOT / "models")
os.environ["SEED_DEMO_DATA"] = "true"
os.environ["RATE_LIMIT_PER_MINUTE"] = "10000"


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def pytest_sessionfinish(_session: pytest.Session, _exitstatus: int) -> None:
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
