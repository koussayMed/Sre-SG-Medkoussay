"""Logs JSON sur stdout, une ligne par événement, avec un request_id.

stdout est collecté par Container Apps vers Log Analytics
(table ContainerAppConsoleLogs_CL) : pas de fichier de log dans le conteneur.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from flask import g, has_request_context

# Attributs standard d'un LogRecord : tout le reste vient de `extra=` et part dans le JSON
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class RequestIdFilter(logging.Filter):
    """Ajoute le request_id de la requête en cours à chaque log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = g.get("request_id") if has_request_context() else None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": "backend",
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class StdoutHandler(logging.StreamHandler):
    """Écrit sur le sys.stdout courant à chaque log (reste compatible avec la capture des tests)."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


def configure_logging(level: str = "INFO") -> None:
    handler = StdoutHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Le log d'accès est fait par l'app (avec request_id et durée) : on coupe celui de Werkzeug
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
