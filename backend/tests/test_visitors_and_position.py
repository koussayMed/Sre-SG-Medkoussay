"""Listes par visiteur (lieux et cache partagés) et météo à la position du visiteur."""

from fakes import OTHER_VISITOR

from app.weather_service import TTLCache


def as_other(client):
    return {"headers": {"X-Visitor-Id": OTHER_VISITOR}}


# --- Listes par visiteur ---------------------------------------------------------------------
def test_missing_or_invalid_visitor_id_is_rejected(app):
    raw = app.test_client()  # sans l'en-tête posé par la fixture `client`
    assert raw.get("/api/weather").status_code == 400
    res = raw.get("/api/weather", headers={"X-Visitor-Id": "pas-un-uuid"})
    assert res.status_code == 400
    assert "X-Visitor-Id" in res.get_json()["message"]


def test_each_visitor_sees_only_their_cities(client, lille):
    assert [c["name"] for c in client.get("/api/weather").get_json()] == ["Lille"]
    assert client.get("/api/weather", **as_other(client)).get_json() == []


def test_same_city_is_shared_between_visitors(client, repo, weather_client, lille):
    client.get("/api/weather")  # remplit le cache pour Lille
    other = client.post("/api/cities", json={"name": "Lille"}, **as_other(client))
    assert other.status_code == 201
    assert other.get_json()["id"] == lille["id"]  # même lieu, pas de doublon
    assert len(repo.cities) == 1

    body = client.get("/api/weather", **as_other(client)).get_json()
    assert body[0]["weather"]["source"] == "cache"  # cache partagé : pas de nouvel appel externe
    assert weather_client.weather_calls == 1


def test_delete_only_removes_from_own_list(client, lille):
    client.post("/api/cities", json={"name": "Lille"}, **as_other(client))
    assert client.delete(f"/api/cities/{lille['id']}").status_code == 204
    assert client.get("/api/weather").get_json() == []
    assert [c["name"] for c in client.get("/api/weather", **as_other(client)).get_json()] == ["Lille"]


def test_city_limit_is_per_visitor(client, repo, settings):
    from fakes import VISITOR

    from app.models import GeoResult

    for i in range(settings.max_cities):
        repo.add_city(GeoResult(500 + i, f"Ville{i}", None, 0.0, 0.0), VISITOR)
    assert client.post("/api/cities", json={"name": "Lille"}).status_code == 409  # limite atteinte
    # la limite d'un visiteur ne touche pas les autres
    assert client.post("/api/cities", json={"name": "Lille"}, **as_other(client)).status_code == 201


# --- Météo à la position -----------------------------------------------------------------------
def test_weather_here_rounds_coordinates_and_caches(client, weather_client):
    first = client.get("/api/weather/here?lat=50.636789&lon=3.063456")
    assert first.status_code == 200
    body = first.get_json()
    assert (body["latitude"], body["longitude"]) == (50.64, 3.06)
    assert body["source"] == "live"
    assert body["description"] == "Couvert"

    # Un voisin à quelques centaines de mètres partage la même entrée de cache
    second = client.get("/api/weather/here?lat=50.6381&lon=3.0612").get_json()
    assert second["source"] == "cache"
    assert weather_client.weather_calls == 1


def test_weather_here_does_not_need_visitor_id(app):
    assert app.test_client().get("/api/weather/here?lat=48.85&lon=2.35").status_code == 200


def test_weather_here_validates_parameters(client):
    for query in ("", "?lat=abc&lon=2", "?lat=91&lon=2", "?lat=45&lon=-181"):
        res = client.get(f"/api/weather/here{query}")
        assert res.status_code == 400, query


def test_weather_here_degrades_when_api_down(client, app, weather_client):
    client.get("/api/weather/here?lat=45.0&lon=5.0")
    service = app.extensions["weather_service"]
    service.ttl = service.ttl * 0  # force l'expiration
    weather_client.down = True
    assert client.get("/api/weather/here?lat=45.0&lon=5.0").get_json()["source"] == "stale"
    assert client.get("/api/weather/here?lat=10.0&lon=10.0").status_code == 503


def test_coordinates_never_appear_in_logs(capsys, client):
    capsys.readouterr()
    client.get("/api/weather/here?lat=50.636789&lon=3.063456")
    out = capsys.readouterr().out
    assert "50.63" not in out and "50.64" not in out


def test_ttl_cache_evicts_oldest_and_reports_age():
    now = [0.0]
    cache = TTLCache(max_entries=2, clock=lambda: now[0])
    cache.set("a", 1)
    cache.set("b", 2)
    now[0] = 5.0
    assert cache.get("a") == (1, 5.0)  # « a » devient le plus récent
    cache.set("c", 3)  # évince « b », le moins récemment utilisé
    assert cache.get("b") is None
    assert cache.get("a") is not None and cache.get("c") is not None
