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

from .errors import DatabaseUnavailable, WeatherApiUnavailable
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

        # API connue comme en panne (circuit ouvert) : inutile de prendre le bail, on sert la dernière valeur.
        # Sinon l'UI afficherait « mise à jour en cours » pendant toute la panne, ce qui serait faux.
        if cached and not self._api_available():
            return cached, "stale"

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

    def _api_available(self) -> bool:
        breaker = getattr(self.client, "breaker", None)
        return breaker is None or breaker.allow()  # half_open : un appel d'essai passe

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
    """Suggestions de villes pendant la saisie, avec cache à deux niveaux.

    Une saisie déclenche une requête par pause de frappe : sans cache, chaque visiteur consommerait le
    quota de l'API de géocodage. Les résultats changent rarement : on les garde 24 h par texte saisi.

    1. Cache mémoire du processus : le plus rapide, mais vide au démarrage d'un réplica.
    2. Cache en base (table geocode_cache), partagé : un réplica ajouté par l'autoscaling réutilise ce
       que les autres ont déjà trouvé, au lieu de rappeler l'API.
    3. Open-Meteo, en dernier recours. Un seul appel à la fois par texte et par processus : les
       requêtes simultanées pour « par » attendent le résultat du premier appel (sans cela, un test de
       charge a provoqué des centaines d'appels identiques, puis des refus 429 de l'API).
    API indisponible : on sert le dernier résultat connu, même expiré, s'il existe.
    """

    _STRIPES = 64  # verrous par « tranche » de textes : nombre borné, quelle que soit la saisie

    def __init__(
        self,
        client,
        repository=None,
        ttl_s: int = 24 * 3600,
        max_entries: int = 2048,
        monotonic=time.monotonic,
        wait_s: float = 8.0,
    ):
        self.client = client
        self.repository = repository
        self.ttl_s = ttl_s
        self.wait_s = wait_s  # attente max du résultat d'un appel déjà en cours
        self.cache = TTLCache(max_entries=max_entries, clock=monotonic)
        self._locks = [threading.Lock() for _ in range(self._STRIPES)]

    @staticmethod
    def _key(text: str) -> str:
        return " ".join(text.split()).casefold()

    def _fresh(self, entry) -> bool:
        return entry is not None and entry[1] < self.ttl_s

    def _read_shared(self, key: str):
        if self.repository is None:
            return None
        try:
            return self.repository.get_geocode(key)
        except DatabaseUnavailable:
            return None  # les suggestions ne dépendent pas de la base : on passe à l'API

    def _write_shared(self, key: str, results: list) -> None:
        if self.repository is None:
            return
        try:
            self.repository.save_geocode(key, results)
        except DatabaseUnavailable:
            log.info("geocode cache not saved", extra={"reason": "database_unavailable"})

    def suggest(self, text: str) -> list:
        key = self._key(text)
        memory = self.cache.get(key)
        if self._fresh(memory):
            return memory[0]

        lock = self._locks[hash(key) % self._STRIPES]
        if not lock.acquire(timeout=self.wait_s):
            if memory:
                return memory[0]
            raise WeatherApiUnavailable("Suggestions momentanément indisponibles")
        try:
            # Pendant l'attente, un autre thread a peut-être déjà obtenu le résultat
            memory = self.cache.get(key)
            if self._fresh(memory):
                return memory[0]
            shared = self._read_shared(key)
            if self._fresh(shared):
                self.cache.set(key, shared[0])
                return shared[0]
            try:
                results = self.client.search(text, count=8)
            except WeatherApiUnavailable:
                stale = memory or shared
                if stale:
                    log.info("serving stale suggestions", extra={"dependency": "open-meteo-geocoding"})
                    return stale[0]
                raise
            self.cache.set(key, results)
            self._write_shared(key, results)
            return results
        finally:
            lock.release()

    def resolve(self, name: str, external_id: int | None = None):
        """La ville choisie dans les suggestions (par identifiant), sinon le meilleur résultat pour ce nom.

        Les coordonnées viennent toujours de l'API, jamais du navigateur : un client ne peut pas
        enregistrer un lieu inventé.
        """
        results = self.suggest(name)
        if external_id is None:
            return results[0] if results else None
        return next((r for r in results if r.external_id == external_id), None)
