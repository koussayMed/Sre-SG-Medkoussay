"""Configuration gunicorn, surchargeable par variables d'environnement."""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"  # noqa: S104 - écoute dans le conteneur
worker_class = "gthread"
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))  # = taille du pool BD par worker

timeout = 30
# Container Apps envoie SIGTERM puis SIGKILL 30 s plus tard : finir les requêtes en cours avant
graceful_timeout = 20
keepalive = 5

# Recycle périodiquement les workers (limite l'effet d'une fuite mémoire)
max_requests = 1000
max_requests_jitter = 100

worker_tmp_dir = "/dev/shm"  # noqa: S108 - heartbeat des workers en mémoire, pas sur le disque du conteneur
accesslog = None  # le log d'accès est émis par l'app (JSON + request_id + durée)
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
