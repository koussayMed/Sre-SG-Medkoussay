# syntax=docker/dockerfile:1

# --- Étape 1 : dépendances dans un virtualenv ---------------------------------
FROM python:3.13-slim AS build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt .
RUN pip install -r requirements.txt

# --- Étape 2 : image d'exécution minimale, sans outils de build --------------
FROM python:3.13-slim
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Durcissement :
# - correctifs de sécurité Debian publiés depuis la construction de l'image de base ;
# - pip retiré : inutile à l'exécution, et une surface d'attaque (et des CVE) en moins.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app

COPY --from=build /opt/venv /opt/venv
RUN /opt/venv/bin/python -m pip uninstall -y pip
COPY app/ app/
COPY wsgi.py gunicorn.conf.py ./

# Version (SHA git) injectée par la CI : exposée par /api/info et vérifiée après déploiement
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

USER 10001
EXPOSE 8000

# Utilisé par docker compose ; Container Apps utilise ses propres sondes HTTP
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

# Migrations (verrou Postgres : sûr avec plusieurs réplicas), puis gunicorn en PID 1 (reçoit SIGTERM)
CMD ["sh", "-c", "python -m app.migrate && exec gunicorn -c gunicorn.conf.py wsgi:app"]
