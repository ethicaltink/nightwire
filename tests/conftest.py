import os
import sys
from pathlib import Path

# Import the app without triggering the module-level create_app() that uvicorn
# relies on, so tests supply their own settings.
os.environ["NVR_SKIP_AUTOSTART"] = "1"
# Route-level tests must not spawn ffmpeg. Tests that care about recorder
# state register a fake supervisor through `recorders.register`.
os.environ["NVR_DISABLE_RECORDER"] = "1"
os.environ["NVR_DISABLE_LIVE"] = "1"
os.environ["NVR_DISABLE_EVENTS"] = "1"
os.environ["NVR_DISABLE_EXPORTS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest
from fastapi.testclient import TestClient

from nightwire import config, health, recorders
from nightwire.main import create_app

TEST_PASSWORD = "correct-horse"
TEST_SECRET = "s" * 40


@pytest.fixture
def password() -> str:
    return TEST_PASSWORD


@pytest.fixture
def settings(tmp_path) -> config.Settings:
    return config.Settings(
        password=TEST_PASSWORD,
        secret_key=TEST_SECRET,
        port=8080,
        timezone="UTC",
        config_dir=tmp_path / "config",
        recordings_dir=tmp_path / "recordings",
        static_dir=tmp_path / "static",
    )


@pytest.fixture
def recordings(settings):
    """The entrypoint creates these in production; mirror that here."""
    settings.recordings_dir.mkdir(parents=True, exist_ok=True)
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    return settings.recordings_dir


@pytest.fixture
def client(settings, recordings):
    # Both registries are process-global by design, so reset them around every
    # test or a probe registered in one test leaks into the next.
    health.reset()
    recorders.reset()
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
    health.reset()
    recorders.reset()


@pytest.fixture
def signed_in(client):
    response = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert response.status_code == 200
    return client
