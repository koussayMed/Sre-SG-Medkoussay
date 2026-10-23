"""Suggestions de villes pendant la saisie (/api/geocode) et ajout de la suggestion choisie."""

import pytest
from fakes import KNOWN_PLACES

from app.models import GeoResult
from app.weather_client import CircuitBreaker, OpenMeteoClient

SFAX = GeoResult(2467454, "Sfax", "Tunisie", 34.74, 10.76, "Sfax")
SFANTU = GeoResult(667268, "Sfântu Gheorghe", "Roumanie", 45.87, 25.79)
SAINT_DENIS_IDF = GeoResult(2980916, "Saint-Denis", "France", 48.94, 2.36, "Île-de-France")
SAINT_DENIS_REUNION = GeoResult(935264, "Saint-Denis", "France", -20.88, 55.45, "La Réunion")


@pytest.fixture(autouse=True)
def more_places(monkeypatch):
    # Deux villes qui commencent par « sf », et deux homonymes « Saint-Denis »
    monkeypatch.setitem(KNOWN_PLACES, "sfax", SFAX)
    monkeypatch.setitem(KNOWN_PLACES, "sfântu gheorghe", SFANTU)
    monkeypatch.setitem(KNOWN_PLACES, "saint-denis", SAINT_DENIS_IDF)
    monkeypatch.setitem(KNOWN_PLACES, "saint-denis (réunion)", SAINT_DENIS_REUNION)


def test_suggestions_by_prefix(client):
    res = client.get("/api/geocode?q=sf")
    assert res.status_code == 200
    names = [s["name"] for s in res.get_json()]
    assert names == ["Sfax", "Sfântu Gheorghe"]
    assert res.get_json()[0] == {"id": 2467454, "name": "Sfax", "region": "Sfax", "country": "Tunisie"}


def test_suggestions_are_cached(client, weather_client):
    client.get("/api/geocode?q=sf")
    client.get("/api/geocode?q=SF")  # même texte, casse différente : même entrée de cache
    client.get("/api/geocode?q=%20sf%20")
    assert weather_client.search_calls == 1


@pytest.mark.parametrize("query", ["", "s", "%20%20"])
def test_too_short_returns_empty_without_api_call(client, weather_client, query):
    res = client.get(f"/api/geocode?q={query}")
    assert res.status_code == 200
    assert res.get_json() == []
    assert weather_client.search_calls == 0


def test_invalid_query_rejected(client):
    assert client.get("/api/geocode?q=<script>").status_code == 400


def test_suggestions_when_api_down(client, weather_client):
    weather_client.down = True
    res = client.get("/api/geocode?q=sf")
    assert res.status_code == 503
    assert res.get_json()["error"] == "weather_api_unavailable"


def test_add_chosen_suggestion_by_id(client):
    # Le visiteur tape « saint-denis » et choisit celui de La Réunion, pas le premier résultat
    client.get("/api/geocode?q=saint-denis")
    res = client.post("/api/cities", json={"name": "saint-denis", "id": 935264})
    assert res.status_code == 201
    body = res.get_json()
    assert (body["name"], body["latitude"]) == ("Saint-Denis", -20.88)


def test_add_without_id_takes_best_match(client):
    res = client.post("/api/cities", json={"name": "saint-denis"})
    assert res.get_json()["latitude"] == 48.94


def test_add_with_unknown_id_is_404(client):
    res = client.post("/api/cities", json={"name": "sfax", "id": 123})
    assert res.status_code == 404


@pytest.mark.parametrize("bad_id", ["935264", 1.5, True])
def test_add_with_invalid_id_type_is_400(client, bad_id):
    res = client.post("/api/cities", json={"name": "sfax", "id": bad_id})
    assert res.status_code == 400


def test_client_search_parses_region():
    class Session:
        def get(self, url, params=None, timeout=None):
            class R:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return {
                        "results": [
                            {
                                "id": 1,
                                "name": "Sfax",
                                "country": "Tunisie",
                                "admin1": "Sfax",
                                "latitude": 34.7,
                                "longitude": 10.7,
                            }
                        ]
                    }

            assert params["count"] == 8
            return R()

    client = OpenMeteoClient("https://f", "https://g", session=Session(), breaker=CircuitBreaker())
    [geo] = client.search("sfa")
    assert (geo.name, geo.region, geo.country) == ("Sfax", "Sfax", "Tunisie")
