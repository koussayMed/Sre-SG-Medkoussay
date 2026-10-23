#!/usr/bin/env sh
# Scan de sécurité local (Linux / macOS / Git Bash) avec Trivy, lancé dans un conteneur.
# Prérequis : `docker compose build` déjà fait. Usage : ./scripts/scan.sh
#
# Version épinglée : la 0.69.4 (et les images Docker Hub 0.69.5/0.69.6) ont été compromises en mars 2026
# (CVE-2026-33634). N'utilisez jamais `latest` sans vérifier la version sur la page des releases.
set -u
TRIVY_IMAGE="aquasec/trivy:0.69.3"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
trivy() {
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v trivy-cache:/root/.cache/ \
    -v "$ROOT:/src:ro" \
    "$TRIVY_IMAGE" "$@"
}

failed=0
for image in sre-meteo-backend sre-meteo-frontend; do
  printf "\n%s\n" "=== Vulnérabilités : $image (bloquant : HIGH/CRITICAL avec correctif) ==="
  trivy image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed \
    --ignorefile /src/.trivyignore --exit-code 1 "$image" || failed=1
done

printf "\n%s\n" "=== Mauvaises configurations : Dockerfiles ==="
trivy config --severity HIGH,CRITICAL --exit-code 1 /src || failed=1

printf "\n%s\n" "=== Secrets commités par erreur ==="
trivy fs --scanners secret --skip-dirs /src/backend/.venv --skip-files /src/.env --exit-code 1 /src || failed=1

[ "$failed" -eq 0 ] && printf "\nOK : aucune vulnérabilité bloquante.\n" || { printf "\nÉCHEC : voir ci-dessus.\n"; exit 1; }
