import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def auth_headers():
    from app.config import settings

    return {"Authorization": f"Bearer {settings.api_access_token}"}
