"""Pytest fixtures: isolated temp DB per test session, seeded demo data."""
from __future__ import annotations

import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["REPORTBUILDER_DB"] = _tmp.name
os.environ["REPORTBUILDER_DB_URL"] = f"sqlite:///{_tmp.name}"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.seed import seed  # noqa: E402


@pytest.fixture(autouse=True)
def _seeded_db():
    # Reseed before every test: admin endpoints can retire fields / revoke
    # scopes / publish versions, and none of that may leak across tests.
    seed()
    yield


@pytest.fixture()
def client():
    return TestClient(app)


def auth(user: str) -> dict[str, str]:
    return {"X-Username": user}


@pytest.fixture()
def h():
    return auth


BASIC_DEF = {
    "metrics": [{"metric_key": "revenue"}],
    "dimensions": ["by_region"],
    "filters": [],
    "scope_key": "scope_full",
}


@pytest.fixture()
def basic_def():
    import copy
    return copy.deepcopy(BASIC_DEF)
