"""Météo des villes avec cache en base, anti-rafale et mode dégradé.

Pour une ville, la source renvoyée dit d'où vient la donnée :
- "cache"      : cache encore valide (< TTL), aucun appel externe ;
- "live"       : cache expiré, cette instance a obtenu le bail et rafraîchi via Open-Meteo ;
- "refreshing" : cache expiré, une autre instance est déjà en train de le rafraîchir
                 (on sert la dernière valeur au lieu d'appeler l'API en même temps qu'elle) ;
- "stale"      : cache expiré et rafraîchissement impossible (API en panne, circuit ouvert,
                 ou budget de temps de la requête épuisé) : dernière valeur connue.
Sans aucune valeur en cache et sans API : WeatherApiUnavailable (503 ou entrée en erreur).

Pourquoi le bail : à l'expiration du TTL sous charge, des dizaines de requêtes simultanées
(sur plusieurs réplicas) appelleraient l'API pour la même ville. Le bail, pris par un UPDATE
atomique en base, garantit un seul appel par ville et par période. Cela protège la latence
et le quota de l'API gratuite (Open-Meteo : usage non commercial, ~10 000 appels/jour).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .errors import WeatherApiUnavailable
from .models import City, Weather

log = logging.getLogger(__name__)

_NOT_LOADED = object()


class TTLCache:
    """Petit cache mémoire, par processus, borné en taille (LRU) et en durée de vie.

    Sert à la météo « à ma position » : les coordonnées arrondies à ~1 km forment la clé, donc les
    visiteurs proches partagent la même entrée. Pas en base : ce sont des coordonnées de visiteurs,
    on ne les conserve pas, et la perte du cache au redémarrage d'un réplica est sans conséquence.
    """

    def __init__(self, max_entries: int = 1024, clock: Callable[[], float] = time.monotonic):
        self.max_entries = max_entries
        self._clock = clock
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key) -> tuple[object, float] | None:
        """(valeur, âge en secondes) ou None."""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            self._data.move_to_end(key)
            value, stored_at = item
            return value, self._clock() - stored_at

    def set(self, key, value) -> None:
        with self._lock:
            self._data[key] = (value, self._clock())
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)


@dataclass(frozen=True)
class CityWeather:
    city: City
    weather: Weather | None
    source: str | None
    error: str | None = None


class WeatherService:
    def __init__(
        self,
        repository,
        client,
        ttl_s: int,
        lease_s: float = 10.0,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.repository = repository
        self.client = client
        self.ttl = timedelta(seconds=ttl_s)
        self.lease_s = lease_s
        self._now = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self.position_cache = TTLCache(clock=monotonic)

    def weather_for(self, city: City, cached=_NOT_LOADED, allow_refresh: bool = True) -> tuple[Weather, str]:
        if cached is _NOT_LOADED:
            cached = self.repository.get_cached_weather(city.id)

        if cached and self._now() - cached.fetched_at < self.ttl:
            return cached, "cache"

        if not allow_refresh:
            if cached:
                return cached, "stale"
            raise WeatherApiUnavailable("Mise à jour reportée : réessayez dans un instant")

        # Une seule instance rafraîchit une ville donnée ; les autres servent la dernière valeur
        if cached and not self.repository.try_acquire_refresh(city.id, self.lease_s):
            return cached, "refreshing"

        try:
            fresh = self.client.current_weather(city.latitude, city.longitude)
        except WeatherApiUnavailable:
            if cached:
                log.warning("serving stale weather", extra={"city_id": city.id})
                return cached, "stale"
            raise

        self.repository.save_weather(city.id, fresh)  # libère le bail
        return fresh, "live"

    def weather_at(self, latitude: float, longitude: float) -> tuple[Weather, str]:
        """Météo à des coordonnées (déjà arrondies par l'appelant), avec cache mémoire et mode dégradé."""
        key = (latitude, longitude)
        cached = self.position_cache.get(key)
        if cached and cached[1] < self.ttl.total_seconds():
            return cached[0], "cache"
        try:
            fresh = self.client.current_weather(latitude, longitude)
        except WeatherApiUnavailable:
            if cached:
                return cached[0], "stale"
            raise
        self.position_cache.set(key, fresh)
        return fresh, "live"

    def all_cities_weather(self, visitor_id: str, budget_s: float) -> list[CityWeather]:
        """Toutes les villes en UNE requête SQL, puis rafraîchissement des caches expirés.

        Budget de temps : au-delà de `budget_s`, on ne rafraîchit plus (on sert le cache, même
        expiré). La requête reste sous les timeouts de nginx même si l'API météo est lente.
        """
        deadline = self._monotonic() + budget_s
        results = []
        for city, cached in self.repository.list_cities_with_weather(visitor_id):
            try:
                weather, source = self.weather_for(city, cached, allow_refresh=self._monotonic() < deadline)
                results.append(CityWeather(city, weather, source))
            except WeatherApiUnavailable as exc:
                results.append(CityWeather(city, None, None, exc.message))
        return results


class PlaceSearch:
    """Suggestions de villes pendant la saisie, avec cache.

    Une saisie déclenche une requête par pause de frappe : sans cache, chaque visiteur consommerait le quota
    de l'API de géocodage. Les résultats changent rarement, on les garde donc 24 h par texte saisi.
    """

    def __init__(self, client, ttl_s: int = 24 * 3600, max_entries: int = 2048, monotonic=time.monotonic):
        self.client = client
        self.ttl_s = ttl_s
        self.cache = TTLCache(max_entries=max_entries, clock=monotonic)

    @staticmethod
    def _key(text: str) -> str:
        return " ".join(text.split()).casefold()

    def suggest(self, text: str) -> list:
        key = self._key(text)
        cached = self.cache.get(key)
        if cached and cached[1] < self.ttl_s:
            return cached[0]
        results = self.client.search(text, count=8)
        self.cache.set(key, results)
        return results

    def resolve(self, name: str, external_id: int | None = None):
        """La ville choisie dans les suggestions (par identifiant), sinon le meilleur résultat pour ce nom.

        Les coordonnées viennent toujours de l'API, jamais du navigateur : un client ne peut pas
        enregistrer un lieu inventé.
        """
        results = self.suggest(name)
        if external_id is None:
            return results[0] if results else None
        return next((r for r in results if r.external_id == external_id), None)
