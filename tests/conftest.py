import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def app_client():
    from hmi.app import create_app
    from hmi.service import HmiService

    service = HmiService([], sampling_ms=100)
    app = create_app(service)
    app.config.update(TESTING=True)
    yield app.test_client()
    service.stop()