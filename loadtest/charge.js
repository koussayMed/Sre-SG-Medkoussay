// Test de charge k6 : monte progressivement jusqu'à VUS utilisateurs simultanés sur l'URL publique,
// pour vérifier que l'application tient (latence, erreurs) et qu'Azure ajoute des réplicas.
//
// Lancement (PowerShell, depuis la racine du dépôt, Docker démarré) :
//   docker run --rm -p 5665:5665 -v "${PWD}\loadtest:/scripts" `
//     -e BASE_URL=https://<votre-frontend>.azurecontainerapps.io `
//     -e K6_WEB_DASHBOARD=true -e K6_WEB_DASHBOARD_EXPORT=/scripts/results/rapport.html `
//     grafana/k6:2.3.0 run --summary-export=/scripts/results/resume.json /scripts/charge.js
// Tableau de bord en direct : http://localhost:5665 ; rapport HTML et résumé JSON dans loadtest/results/.
//
// Paramètres (-e NOM=valeur) : VUS (100), RAMP (2m), PLATEAU (4m).
//
// Pourquoi ce scénario ne surcharge pas l'API Open-Meteo : toutes les requêtes portent sur 8 villes.
// La météo est en cache partagé (10 min) et les suggestions en cache 24 h : quelques dizaines d'appels
// externes au plus, quel que soit le nombre de requêtes envoyées à l'application.

import http from "k6/http";
import { check, fail, sleep } from "k6";

const BASE_URL = (__ENV.BASE_URL || "").replace(/\/+$/, "");
const VUS = parseInt(__ENV.VUS || "100", 10);
const RAMP = __ENV.RAMP || "2m";
const PLATEAU = __ENV.PLATEAU || "4m";

const VISITORS = 50; // visiteurs simulés, chacun avec sa liste de 3 villes
const CITIES = ["Paris", "Lyon", "Marseille", "Lille", "Bordeaux", "Nantes", "Tunis", "Sfax"];
const SEARCHES = ["par", "lyo", "mars", "lil", "bord", "nan", "tun", "sfa"];

export const options = {
  scenarios: {
    montee_en_charge: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: Math.max(1, Math.ceil(VUS / 5)) }, // échauffement
        { duration: RAMP, target: VUS }, // montée : la file d'attente grossit, Azure ajoute des réplicas
        { duration: PLATEAU, target: VUS }, // plateau : la latence doit redescendre une fois les réplicas prêts
        { duration: "30s", target: 0 },
      ],
      gracefulRampDown: "10s",
    },
  },
  // Objectifs de service, mesurés pendant la charge seulement (la préparation est exclue).
  // k6 les affiche en vert ou rouge à la fin et se termine en erreur s'ils ne sont pas tenus.
  thresholds: {
    "http_req_failed{scenario:montee_en_charge}": ["rate<0.01"], // moins de 1 % d'erreurs
    // Même objectif par route : le résumé montre directement laquelle décroche
    "http_req_failed{endpoint:weather}": ["rate<0.01"],
    "http_req_failed{endpoint:geocode}": ["rate<0.01"],
    "http_req_duration{endpoint:weather}": ["p(95)<1500"], // 95 % des listes météo en moins de 1,5 s
    "checks{scenario:montee_en_charge}": ["rate>0.99"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

// Identifiants visiteurs fixes (format UUID attendu par l'API) : relancer le test réutilise les mêmes listes
function visitorId(n) {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

function pick(list) {
  return list[Math.floor(Math.random() * list.length)];
}

export function setup() {
  if (!BASE_URL.startsWith("http")) {
    fail("BASE_URL manquante : -e BASE_URL=https://...");
  }
  const health = http.get(`${BASE_URL}/healthz`);
  if (health.status !== 200) {
    fail(`application injoignable (${BASE_URL}/healthz : ${health.status})`);
  }
  // Chaque visiteur suit 3 villes (201 = ajoutée, 409 = déjà présente lors d'un test précédent)
  let ok = 0;
  for (let n = 1; n <= VISITORS; n++) {
    for (let k = 0; k < 3; k++) {
      const res = http.post(
        `${BASE_URL}/api/cities`,
        JSON.stringify({ name: CITIES[(n + k) % CITIES.length] }),
        {
          headers: { "Content-Type": "application/json", "X-Visitor-Id": visitorId(n) },
          tags: { endpoint: "setup" },
          responseCallback: http.expectedStatuses(201, 409),
        }
      );
      if (res.status === 201 || res.status === 409) ok++;
    }
  }
  if (ok === 0) fail("impossible de préparer les listes de villes");
  console.log(`préparation : ${ok}/${VISITORS * 3} villes en place pour ${VISITORS} visiteurs`);
}

// Parcours d'un visiteur, répété en boucle par chaque utilisateur virtuel
export default function () {
  const visitor = visitorId(1 + ((__VU + __ITER) % VISITORS));
  const roll = Math.random();

  if (roll < 0.7) {
    // Cas principal : la page d'accueil charge la liste du visiteur avec la météo (1 requête SQL agrégée)
    const res = http.get(`${BASE_URL}/api/weather`, {
      headers: { "X-Visitor-Id": visitor },
      tags: { endpoint: "weather" },
    });
    check(res, {
      "météo : 200": (r) => r.status === 200,
      "météo : liste de villes": (r) => r.status === 200 && Array.isArray(r.json()) && r.json().length > 0,
    });
  } else if (roll < 0.85) {
    // Saisie dans le champ de recherche : suggestions
    const res = http.get(`${BASE_URL}/api/geocode?q=${pick(SEARCHES)}`, { tags: { endpoint: "geocode" } });
    check(res, { "suggestions : 200": (r) => r.status === 200 });
  } else if (roll < 0.95) {
    // Page statique servie par nginx (ne touche pas le backend)
    const res = http.get(`${BASE_URL}/`, { tags: { endpoint: "page" } });
    check(res, { "page : 200": (r) => r.status === 200 });
  } else {
    const res = http.get(`${BASE_URL}/api/info`, { tags: { endpoint: "info" } });
    check(res, { "info : 200": (r) => r.status === 200 });
  }

  sleep(Math.random() * 0.2); // très peu de pause : beaucoup de requêtes en vol, pour déclencher le scaling
}
