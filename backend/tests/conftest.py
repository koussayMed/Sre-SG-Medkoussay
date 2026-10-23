import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # rend `fakes` importable

from fakes import VISITOR, FakeRepository, FakeWeatherClient  # noqa: E402

from app import create_app  # noqa: E402
from app.config import Settings  # noqa: E402


@pytest.fixture
def settings():
    return Settings(app_version="test-sha", environment="test", max_cities=3)


@pytest.fixture
def repo():
    return FakeRepository()


@pytest.fixture
def weather_client():
    return FakeWeatherClient()


@pytest.fixture
def app(settings, repo, weather_client):
    app = create_app(settings, repository=repo, weather_client=weather_client)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    """Client de test qui envoie l'identifiant de visiteur, comme le front."""
    test_client = app.test_client()
    test_client.environ_base["HTTP_X_VISITOR_ID"] = VISITOR
    return test_client


@pytest.fixture
def lille(client):
    """Ajoute Lille et renvoie son JSON."""
    return client.post("/api/cities", json={"name": "Lille"}).get_json()
