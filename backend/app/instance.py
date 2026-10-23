"""Identité de l'instance qui sert la requête.

Container Apps expose le nom de révision et de réplica en variables
d'environnement (à vérifier en déploiement) ; en local on retombe sur le hostname.
Afficher ces valeurs dans l'UI rend la répartition de charge et le scale-out visibles.
"""

from __future__ import annotations

import os
import socket
import time

_STARTED_AT = time.time()


def instance_info() -> dict:
    return {
        "replica": os.environ.get("CONTAINER_APP_REPLICA_NAME") or socket.gethostname(),
        "revision": os.environ.get("CONTAINER_APP_REVISION", "local"),
        "pid": os.getpid(),
        "uptime_s": round(time.time() - _STARTED_AT, 1),
    }
