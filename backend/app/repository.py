"""Accès PostgreSQL (psycopg 3 + pool de connexions).

psycopg n'est importé qu'ici : les tests unitaires utilisent un faux repository
et tournent sans base. Les tests d'intégration (tests/integration) visent un vrai Postgres.
"""

from __future__ import annotations

import atexit
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from .errors import Conflict, DatabaseUnavailable
from .models import City, GeoResult, Weather

log = logging.getLogger(__name__)

# Requêtes en littéraux constants : les valeurs passent toujours en paramètres (%s), jamais par formatage
# Les lieux (places) sont partagés entre visiteurs ; la liste de chaque visiteur est dans favorites.
_SELECT_PLACE = "SELECT id, external_id, name, country, latitude, longitude FROM places"
_SELECT_VISITOR_PLACES = (
    "SELECT p.id, p.external_id, p.name, p.country, p.latitude, p.longitude "
    "FROM favorites f JOIN places p ON p.id = f.place_id "
    "WHERE f.visitor_id = %s ORDER BY f.created_at, p.id"
)
# Lieu créé s'il n'existe pas encore. Le DO UPDATE (sans effet réel) fait renvoyer la ligne existante
_UPSERT_PLACE = (
    "INSERT INTO places (external_id, name, country, latitude, longitude) VALUES (%s, %s, %s, %s, %s) "
    "ON CONFLICT (external_id) DO UPDATE SET name = EXCLUDED.name "
    "RETURNING id, external_id, name, country, latitude, longitude"
)
# Une seule requête pour toute la liste du visiteur et la météo en cache (évite le N+1)
_SELECT_VISITOR_PLACES_WITH_WEATHER = (
    "SELECT p.id, p.external_id, p.name, p.country, p.latitude, p.longitude, "
    "w.temperature_c, w.wind_kmh, w.weather_code, w.observed_at, w.fetched_at "
    "FROM favorites f JOIN places p ON p.id = f.place_id "
    "LEFT JOIN weather_cache w ON w.city_id = p.id "
    "WHERE f.visitor_id = %s ORDER BY f.created_at, p.id"
)
_UPSERT_WEATHER = (
    "INSERT INTO weather_cache "
    "(city_id, temperature_c, wind_kmh, weather_code, observed_at, fetched_at, refresh_lease_until) "
    "VALUES (%s, %s, %s, %s, %s, %s, NULL) "
    "ON CONFLICT (city_id) DO UPDATE SET "
    "temperature_c = EXCLUDED.temperature_c, wind_kmh = EXCLUDED.wind_kmh, "
    "weather_code = EXCLUDED.weather_code, observed_at = EXCLUDED.observed_at, "
    "fetched_at = EXCLUDED.fetched_at, refresh_lease_until = NULL"
)
# Bail de rafraîchissement : une seule instance (tous réplicas confondus) gagne le droit d'appeler l'API
_ACQUIRE_REFRESH_LEASE = (
    "UPDATE weather_cache SET refresh_lease_until = now() + %s * interval '1 second' "
    "WHERE city_id = %s AND (refresh_lease_until IS NULL OR refresh_lease_until < now())"
)


class PostgresRepository:
    def __init__(
        self, database_url: str, pool_min_size: int = 1, pool_max_size: int = 2, timeout_s: float = 3.0
    ):
        # Créé dans chaque worker gunicorn (pas de --preload) : le pool n'est jamais partagé après fork.
        # max_size borne les connexions ouvertes par worker : c'est la base du budget de connexions.
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=pool_min_size,
            max_size=pool_max_size,
            timeout=timeout_s,  # attente max d'une connexion libre -> 503 plutôt que blocage
            kwargs={"row_factory": dict_row, "connect_timeout": int(timeout_s)},
            check=ConnectionPool.check_connection,  # écarte les connexions mortes (ex. redémarrage de la BD)
            open=False,
            name="app-db",
        )
        # wait=False : l'app démarre même si la BD est down ; /readyz signalera le problème
        self._pool.open(wait=False)
        atexit.register(self._pool.close)

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        try:
            # commit à la sortie du bloc, rollback si exception
            with self._pool.connection() as conn:
                yield conn
        except (PoolTimeout, psycopg.OperationalError) as exc:
            log.error("database unavailable", extra={"error": str(exc)})
            raise DatabaseUnavailable() from exc

    # --- santé ---------------------------------------------------------------
    def ping(self) -> None:
        with self._conn() as conn:
            conn.execute("SELECT 1")

    def pool_stats(self) -> dict:
        """Saturation du pool : requests_waiting > 0 sous charge = goulot côté base."""
        stats = self._pool.get_stats()
        keys = ("pool_min", "pool_max", "pool_size", "pool_available", "requests_waiting")
        return {k: stats.get(k, 0) for k in keys}

    # --- villes ----------------------------------------------------------------
    def list_cities(self, visitor_id: str) -> list[City]:
        with self._conn() as conn:
            rows = conn.execute(_SELECT_VISITOR_PLACES, (visitor_id,)).fetchall()
        return [City(**row) for row in rows]

    def list_cities_with_weather(self, visitor_id: str) -> list[tuple[City, Weather | None]]:
        with self._conn() as conn:
            rows = conn.execute(_SELECT_VISITOR_PLACES_WITH_WEATHER, (visitor_id,)).fetchall()
        result = []
        for row in rows:
            city = City(
                row["id"], row["external_id"], row["name"], row["country"], row["latitude"], row["longitude"]
            )
            weather = None
            if row["fetched_at"] is not None:
                weather = Weather(
                    row["temperature_c"],
                    row["wind_kmh"],
                    row["weather_code"],
                    row["observed_at"],
                    row["fetched_at"],
                )
            result.append((city, weather))
        return result

    def get_city(self, city_id: int) -> City | None:
        with self._conn() as conn:
            row = conn.execute(_SELECT_PLACE + " WHERE id = %s", (city_id,)).fetchone()
        return City(**row) if row else None

    def count_cities(self, visitor_id: str) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM favorites WHERE visitor_id = %s", (visitor_id,)
            ).fetchone()["n"]

    def add_city(self, geo: GeoResult, visitor_id: str) -> City:
        """Ajoute la ville à la liste du visiteur (le lieu est créé une seule fois, puis partagé)."""
        try:
            with self._conn() as conn:  # une transaction : lieu + favori, ou rien
                row = conn.execute(
                    _UPSERT_PLACE, (geo.external_id, geo.name, geo.country, geo.latitude, geo.longitude)
                ).fetchone()
                conn.execute(
                    "INSERT INTO favorites (visitor_id, place_id) VALUES (%s, %s)", (visitor_id, row["id"])
                )
        except pg_errors.UniqueViolation as exc:
            raise Conflict(f"{geo.name} est déjà dans ta liste") from exc
        return City(**row)

    def delete_city(self, city_id: int, visitor_id: str) -> bool:
        """Retire la ville de la liste du visiteur ; le lieu et son cache restent pour les autres."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM favorites WHERE visitor_id = %s AND place_id = %s", (visitor_id, city_id)
            )
            return cur.rowcount > 0

    # --- cache météo -------------------------------------------------------------
    def get_cached_weather(self, city_id: int) -> Weather | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT temperature_c, wind_kmh, weather_code, observed_at, fetched_at "
                "FROM weather_cache WHERE city_id = %s",
                (city_id,),
            ).fetchone()
        return Weather(**row) if row else None

    def try_acquire_refresh(self, city_id: int, lease_s: float) -> bool:
        """True si cette instance obtient le bail (et doit appeler l'API), False si une autre l'a déjà.

        UPDATE atomique : même avec 10 réplicas, une seule requête gagne pendant `lease_s` secondes.
        Si elle plante, le bail expire tout seul : pas de verrou orphelin.
        """
        with self._conn() as conn:
            cur = conn.execute(_ACQUIRE_REFRESH_LEASE, (float(lease_s), city_id))
            return cur.rowcount == 1

    def save_weather(self, city_id: int, weather: Weather) -> None:
        params = (
            city_id,
            weather.temperature_c,
            weather.wind_kmh,
            weather.weather_code,
            weather.observed_at,
            weather.fetched_at,
        )
        try:
            with self._conn() as conn:
                conn.execute(_UPSERT_WEATHER, params)  # libère aussi le bail
        except pg_errors.ForeignKeyViolation:
            # La ville a été supprimée entre-temps : rien à mettre en cache
            log.info("city deleted before cache write", extra={"city_id": city_id})
