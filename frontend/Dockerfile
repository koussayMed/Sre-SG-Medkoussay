# Image nginx non-root (utilisateur 101, port 8080). Vérifier le dernier tag stable sur Docker Hub.
FROM nginxinc/nginx-unprivileged:1.28-alpine

# Correctifs de sécurité Alpine publiés depuis la construction de l'image de base.
# L'image tourne en utilisateur 101 : on repasse root le temps de la mise à jour, puis on revient à 101.
USER root
RUN apk upgrade --no-cache
USER 101

# Le template est rendu au démarrage (envsubst) vers /etc/nginx/conf.d/default.conf
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY public/ /usr/share/nginx/html/

# Adresse du backend : surchargée par l'environnement (compose, Container Apps)
ENV BACKEND_URL=http://backend:8000

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
