"""Factory Flask : configuration, dépendances, logs, corrélation, erreurs JSON."""

from __future__ import annotations

import logging
import re
import time
import uuid

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Settings
from .errors import ApiError
from .logging_setup import configure_logging
from .routes import bp
from .weather_client import CircuitBreaker, OpenMeteoClient
from .weather_service import PlaceSearch, WeatherService

log = logging.getLogger("app")

# X-Request-ID entrant (posé par le front nginx) accepté seulement s'il est court et sûr
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_QUIET_PATHS = {"/healthz", "/readyz"}  # sondes appelées toutes les quelques secondes


def create_app(settings: Settings | None = None, repository=None, weather_client=None) -> Flask:
    """Les dépendances sont injectables : les tests passent des faux, la prod les construit ici."""
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)

    if repository is None:
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL est obligatoire")
        from .repository import PostgresRepository  # import tardif : psycopg inutile pour les tests

        repository = PostgresRepository(
            settings.database_url, settings.db_pool_min_size, settings.db_pool_max_size
        )
        # Rappel du budget au démarrage : max_replicas x cette valeur doit rester < connexions de la BD
        log.info(
            "database pool configured",
            extra={
                "pool_max_per_worker": settings.db_pool_max_size,
                "gunicorn_workers": settings.gunicorn_workers,
                "max_connections_per_replica": settings.db_connections_per_replica,
            },
        )

    if weather_client is None:
        weather_client = OpenMeteoClient(
            forecast_url=settings.forecast_api_url,
            geocoding_url=settings.geocoding_api_url,
            timeout_s=settings.http_timeout_s,
            breaker=CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_reset_after_s),
        )

    app = Flask(__name__, static_folder=None)  # API uniquement : les fichiers statiques sont servis par nginx
    app.config["SETTINGS"] = settings
    app.json.sort_keys = False
    app.json.ensure_ascii = False
    app.extensions["repository"] = repository
    app.extensions["weather_client"] = weather_client
    app.extensions["weather_service"] = WeatherService(
        repository,
        weather_client,
        ttl_s=settings.weather_cache_ttl_s,
        lease_s=settings.refresh_lease_s,
    )
    app.extensions["place_search"] = PlaceSearch(weather_client)

    # Derrière nginx et l'ingress Container Apps : récupérer le vrai schéma et l'IP client
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    app.register_blueprint(bp)
    _register_request_hooks(app)
    _register_error_handlers(app)

    log.info(
        "application started",
        extra={"version": settings.app_version, "environment": settings.environment},
    )
    return app


def _register_request_hooks(app: Flask) -> None:
    @app.before_request
    def start_request():
        incoming = request.headers.get("X-Request-ID", "")
        g.request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        g.started = time.perf_counter()

    @app.after_request
    def finish_request(response):
        response.headers["X-Request-ID"] = g.get("request_id", "")
        response.headers.setdefault("Cache-Control", "no-store")
        # Les en-têtes de sécurité navigateur (CSP, nosniff...) sont posés une seule fois, par nginx

        duration_ms = round((time.perf_counter() - g.get("started", time.perf_counter())) * 1000, 2)
        log.log(
            logging.DEBUG if request.path in _QUIET_PATHS else logging.INFO,
            "request completed",
            extra={
                "method": request.method,
                "path": request.path,
                "route": request.url_rule.rule if request.url_rule else None,  # agrégation par route
                "status": response.status_code,
                "duration_ms": duration_ms,
                "client_ip": request.remote_addr,
            },
        )
        return response


def _error_body(code: str, message: str) -> dict:
    return {"error": code, "message": message, "request_id": g.get("request_id")}


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def api_error(err: ApiError):
        if err.status >= 500:
            log.warning("dependency error", extra={"error": err.code, "detail": err.message})
        return jsonify(_error_body(err.code, err.message)), err.status

    @app.errorhandler(HTTPException)
    def http_error(err: HTTPException):
        code = err.name.lower().replace(" ", "_")
        return jsonify(_error_body(code, err.description or err.name)), err.code

    @app.errorhandler(Exception)
    def unhandled(err: Exception):
        # Trace complète dans les logs, rien de sensible côté client
        log.exception("unhandled error")
        return jsonify(_error_body("internal_error", "Erreur interne")), 500
