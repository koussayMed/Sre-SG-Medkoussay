"""Client de l'API externe Open-Meteo (géocodage + météo actuelle), sans clé.

Une dépendance externe peut être lente ou tomber. Trois protections :
- timeouts courts sur chaque appel (connexion et lecture) ;
- retries bornés, uniquement sur erreurs transitoires (GET idempotent) ;
- circuit breaker : après N échecs consécutifs, on n'appelle plus l'API pendant
  quelques secondes et on échoue tout de suite. Sous charge, cela évite que tous
  les threads gunicorn restent bloqués sur une API en panne.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .errors import WeatherApiUnavailable
from .models import GeoResult, Weather

log = logging.getLogger(__name__)


class CircuitBreaker:
    """Disjoncteur minimal, par processus (l'état n'est pas partagé entre réplicas).

    closed    : les appels passent ;
    open      : trop d'échecs, les appels sont refusés jusqu'à reset_after_s ;
    half_open : délai écoulé, un appel d'essai passe ; succès -> closed, échec -> open.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        reset_after_s: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.failure_threshold = failure_threshold
        self.reset_after_s = reset_after_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if self._clock() - self._opened_at >= self.reset_after_s:
                return "half_open"
            return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                if self._opened_at is None:
                    log.warning("circuit breaker opened", extra={"dependency": "open-meteo"})
                self._opened_at = self._clock()  # (ré)ouvre, y compris après un essai half_open raté


def build_session(retries: int = 2) -> requests.Session:
    retry = Retry(
        total=retries,
        backoff_factor=0.2,
        connect=retries,  # erreurs de connexion : souvent transitoires, on réessaie
        read=0,  # timeout de lecture : une API lente ne sera pas plus rapide, réessayer double la latence
        status=retries,
        # 5xx transitoires seulement : réessayer un 429 (quota) aggraverait le problème
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
        respect_retry_after_header=False,  # un Retry-After de 60 s bloquerait un thread
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = "meteo-api/1.0"
    return session


class OpenMeteoClient:
    def __init__(
        self,
        forecast_url: str,
        geocoding_url: str,
        timeout_s: float = 2.5,
        session: requests.Session | None = None,
        breaker: CircuitBreaker | None = None,
    ):
        self.forecast_url = forecast_url
        self.geocoding_url = geocoding_url
        self.timeout = (min(1.5, timeout_s), timeout_s)  # (connexion, lecture)
        self.session = session or build_session()
        self.breaker = breaker or CircuitBreaker()

    def search(self, name: str, count: int = 8) -> list[GeoResult]:
        """Villes dont le nom correspond (Open-Meteo : correspondance exacte à 2 lettres, approchée dès 3)."""
        data = self._get_json(
            "geocoding",
            self.geocoding_url,
            {"name": name, "count": count, "language": "fr", "format": "json"},
        )
        try:
            return [
                GeoResult(
                    external_id=int(item["id"]),
                    name=str(item["name"]),
                    country=item.get("country"),
                    latitude=float(item["latitude"]),
                    longitude=float(item["longitude"]),
                    region=item.get("admin1"),
                )
                for item in data.get("results") or []
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherApiUnavailable("Réponse de géocodage invalide") from exc

    def geocode(self, name: str) -> GeoResult | None:
        results = self.search(name, count=1)
        return results[0] if results else None

    def current_weather(self, latitude: float, longitude: float) -> Weather:
        data = self._get_json(
            "forecast",
            self.forecast_url,
            {
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,wind_speed_10m,weather_code",
                "timezone": "GMT",
            },
        )
        try:
            current = data["current"]
            return Weather(
                temperature_c=float(current["temperature_2m"]),
                wind_kmh=float(current["wind_speed_10m"]),
                weather_code=current.get("weather_code"),
                observed_at=datetime.fromisoformat(current["time"]).replace(tzinfo=UTC),
                fetched_at=datetime.now(UTC),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherApiUnavailable("Réponse météo invalide") from exc

    def _get_json(self, endpoint: str, url: str, params: dict) -> dict:
        if not self.breaker.allow():
            log.info(
                "dependency call skipped",
                extra={"dependency": "open-meteo", "endpoint": endpoint, "outcome": "circuit_open"},
            )
            raise WeatherApiUnavailable("Service météo externe indisponible (circuit ouvert)")

        started = time.perf_counter()
        outcome = "success"
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("payload JSON inattendu")
        except (requests.RequestException, ValueError) as exc:
            outcome = type(exc).__name__
            self.breaker.record_failure()
            raise WeatherApiUnavailable() from exc
        else:
            self.breaker.record_success()
            return data
        finally:
            log.info(
                "dependency call",
                extra={
                    "dependency": "open-meteo",
                    "endpoint": endpoint,
                    "outcome": outcome,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
