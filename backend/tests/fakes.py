"""Doublures de test : même interface que PostgresRepository et OpenMeteoClient."""

from __future__ import annotations

from datetime import UTC, datetime

from app.errors import Conflict, DatabaseUnavailable, WeatherApiUnavailable
from app.models import City, GeoResult, Weather
from app.weather_client import CircuitBreaker

# Identifiants de visiteurs utilisés par les tests (format UUID, comme ceux générés par le navigateur)
VISITOR = "11111111-1111-4111-8111-111111111111"
OTHER_VISITOR = "22222222-2222-4222-8222-222222222222"


class FakeRepository:
    """Lieux partagés (self.cities) + liste par visiteur (self.favorites), comme en base."""

    def __init__(self):
        self.cities: dict[int, City] = {}  # lieux partagés
        self.favorites: dict[str, list[int]] = {}  # visiteur -> ids de lieux, dans l'ordre d'ajout
        self.cache: dict[int, Weather] = {}
        self.next_id = 1
        self.down = False  # simule une panne de la base
        self.leases: set[int] = set()  # lieux dont le bail de rafraîchissement est pris
        self.queries = 0  # nombre d'appels « SQL » (pour vérifier l'absence de N+1)

    def _check(self):
        self.queries += 1
        if self.down:
            raise DatabaseUnavailable()

    def pool_stats(self):
        return {"pool_min": 1, "pool_max": 2, "pool_size": 1, "pool_available": 1, "requests_waiting": 0}

    def _visitor_places(self, visitor_id):
        return [self.cities[i] for i in self.favorites.get(visitor_id, [])]

    def list_cities_with_weather(self, visitor_id):
        self._check()
        return [(city, self.cache.get(city.id)) for city in self._visitor_places(visitor_id)]

    def try_acquire_refresh(self, city_id, lease_s):
        self._check()
        if city_id not in self.cache or city_id in self.leases:
            return False
        self.leases.add(city_id)
        return True

    def ping(self):
        self._check()

    def list_cities(self, visitor_id):
        self._check()
        return self._visitor_places(visitor_id)

    def get_city(self, city_id):
        self._check()
        return self.cities.get(city_id)

    def count_cities(self, visitor_id):
        self._check()
        return len(self.favorites.get(visitor_id, []))

    def add_city(self, geo: GeoResult, visitor_id):
        self._check()
        city = next((c for c in self.cities.values() if c.external_id == geo.external_id), None)
        if city is None:
            city = City(self.next_id, geo.external_id, geo.name, geo.country, geo.latitude, geo.longitude)
            self.cities[city.id] = city
            self.next_id += 1
        mine = self.favorites.setdefault(visitor_id, [])
        if city.id in mine:
            raise Conflict(f"{geo.name} est déjà dans ta liste")
        mine.append(city.id)
        return city

    def delete_city(self, city_id, visitor_id):
        self._check()
        mine = self.favorites.get(visitor_id, [])
        if city_id not in mine:
            return False
        mine.remove(city_id)
        return True

    def get_cached_weather(self, city_id):
        self._check()
        return self.cache.get(city_id)

    def save_weather(self, city_id, weather):
        self._check()
        if city_id in self.cities:
            self.cache[city_id] = weather
            self.leases.discard(city_id)


KNOWN_PLACES = {
    "lille": GeoResult(2998324, "Lille", "France", 50.63297, 3.05858),
    "paris": GeoResult(2988507, "Paris", "France", 48.85341, 2.3488),
}


class FakeWeatherClient:
    def __init__(self):
        self.down = False  # simule une panne d'Open-Meteo
        self.weather_calls = 0
        self.search_calls = 0
        self.temperature = 14.2
        self.breaker = CircuitBreaker()

    def search(self, name, count=8):
        self.search_calls += 1
        if self.down:
            raise WeatherApiUnavailable()
        text = name.casefold()
        exact = [geo for key, geo in KNOWN_PLACES.items() if key == text]
        prefix = [geo for key, geo in KNOWN_PLACES.items() if key.startswith(text) and key != text]
        return (exact + prefix)[:count]

    def geocode(self, name):
        results = self.search(name, 1)
        return results[0] if results else None

    def current_weather(self, latitude, longitude):
        self.weather_calls += 1
        if self.down:
            raise WeatherApiUnavailable()
        now = datetime.now(UTC)
        return Weather(self.temperature, 18.0, 3, now.replace(second=0, microsecond=0), now)
