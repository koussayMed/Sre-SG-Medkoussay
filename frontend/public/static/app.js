"use strict";

const $ = (selector, root = document) => root.querySelector(selector);

// --- Identifiant anonyme du navigateur ------------------------------------------------------
// Sépare les listes de villes entre visiteurs, sans compte. Ce n'est pas une authentification.
const VISITOR_KEY = "meteo.visitorId";
let memoryVisitorId = null;

function newUuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function visitorId() {
  try {
    let id = localStorage.getItem(VISITOR_KEY);
    if (!id) {
      id = newUuid();
      localStorage.setItem(VISITOR_KEY, id);
    }
    return id;
  } catch {
    // Stockage indisponible (navigation privée stricte) : identifiant valable pour cette page seulement
    memoryVisitorId = memoryVisitorId || newUuid();
    return memoryVisitorId;
  }
}

// --- Appels API ----------------------------------------------------------------------------------
async function api(path, options = {}) {
  const res = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: { "Content-Type": "application/json", "X-Visitor-Id": visitorId(), ...(options.headers || {}) },
  });
  if (res.status === 204) return null;
  let body = null;
  try {
    body = await res.json();
  } catch {
    // réponse non JSON (rare) : on garde le statut HTTP
  }
  if (!res.ok) {
    const message = body?.message || `Erreur HTTP ${res.status}`;
    throw new Error(body?.request_id ? `${message} (référence ${body.request_id.slice(0, 8)})` : message);
  }
  return body;
}

// --- Présentation de la météo ----------------------------------------------------------------
// Codes WMO renvoyés par Open-Meteo -> famille de temps (icône et ciel)
function weatherKind(code) {
  if (code === 0 || code === 1) return "clear";
  if (code === 2) return "partly";
  if (code === 3) return "cloudy";
  if (code === 45 || code === 48) return "fog";
  if ((code >= 51 && code <= 67) || (code >= 80 && code <= 82)) return "rain";
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "snow";
  if (code >= 95) return "storm";
  return "cloudy";
}

function isNight(date = new Date()) {
  const h = date.getHours();
  return h < 7 || h >= 20;
}

function iconFor(kind, night) {
  if (kind === "clear") return night ? "#i-moon" : "#i-sun";
  if (kind === "partly") return night ? "#i-moon" : "#i-partly";
  if (kind === "cloudy") return "#i-cloud";
  return `#i-${kind}`;
}

function skyFor(kind, night) {
  if (kind === "clear" || kind === "partly") return `${kind}-${night ? "night" : "day"}`;
  return kind;
}

const FRESHNESS = {
  live: null,
  cache: null,
  refreshing: "Mise à jour en cours",
  stale: "Données anciennes",
};

function timeLabel(iso) {
  return new Date(iso).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

// Vrai signe moins (U+2212) : le tiret « - » est trop court et mal espacé avec les chiffres
function tempNumber(t) {
  const n = Math.round(t);
  return n < 0 ? `\u2212${Math.abs(n)}` : String(n);
}

function tempLabel(t) {
  return `${tempNumber(t)}°`;
}

// Position sur l'échelle -10 °C -> 40 °C
function scalePosition(t) {
  const pct = ((t + 10) / 50) * 100;
  return `${Math.min(100, Math.max(0, pct))}%`;
}

// --- Météo à la position du visiteur ------------------------------------------------------------
const here = $("#here");

function setHereState(state) {
  here.dataset.state = state;
}

function showHereError(message) {
  $("#here-error").textContent = message;
  here.dataset.sky = "idle";
  setHereState("error");
}

async function loadHereWeather(position) {
  // Arrondi à ~1 km avant tout envoi : la position exacte ne quitte jamais le navigateur
  const lat = position.coords.latitude.toFixed(2);
  const lon = position.coords.longitude.toFixed(2);
  try {
    const w = await api(`/api/weather/here?lat=${lat}&lon=${lon}`);
    const kind = weatherKind(w.weather_code);
    const night = isNight();
    $("#here-temp").textContent = tempNumber(w.temperature_c);
    $("#here-icon").setAttribute("href", iconFor(kind, night));
    $("#here-desc").textContent = w.description;
    $("#here-wind").textContent = `Vent ${Math.round(w.wind_kmh)} km/h`;
    const note = FRESHNESS[w.source];
    $("#here-time").textContent = note
      ? `Relevé à ${timeLabel(w.observed_at)}, ${note.toLowerCase()}`
      : `Relevé à ${timeLabel(w.observed_at)}`;
    here.dataset.sky = skyFor(kind, night);
    setHereState("ready");
  } catch (err) {
    showHereError(`Météo indisponible pour votre position : ${err.message}`);
  }
}

const DENIED_MESSAGE =
  "Localisation refusée. Autorisez-la dans les réglages du navigateur pour voir la météo autour de vous.";

function locate() {
  if (!("geolocation" in navigator)) {
    showHereError("Ce navigateur ne permet pas la géolocalisation.");
    return;
  }
  setHereState("loading");
  navigator.geolocation.getCurrentPosition(
    loadHereWeather,
    (err) => {
      const messages = {
        1: DENIED_MESSAGE,
        2: "Position introuvable pour le moment.",
        3: "La localisation a pris trop de temps.",
      };
      showHereError(messages[err.code] || "Localisation impossible.");
    },
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 10 * 60 * 1000 }
  );
}

async function initHere() {
  // Si le visiteur a déjà accepté, on affiche directement ; sinon on attend son clic
  try {
    const status = await navigator.permissions?.query({ name: "geolocation" });
    if (status?.state === "granted") return locate();
    if (status?.state === "denied") return showHereError(DENIED_MESSAGE);
  } catch {
    // API Permissions absente : on reste sur le bouton
  }
  setHereState("idle");
}

$("#locate-btn").addEventListener("click", locate);
$("#retry-btn").addEventListener("click", locate);

// --- Mes villes ---------------------------------------------------------------------------------
const list = $("#cities");
const template = $("#city-template");
const formMessage = $("#form-message");
const EMPTY_TEXT = "Ajoutez une ville pour suivre sa météo.";

function updateCount() {
  const n = list.children.length;
  $("#count").textContent = n ? `(${n})` : "";
  $("#empty").hidden = n > 0;
}

function showCityWeather(row, w) {
  row.classList.remove("failed");
  const kind = weatherKind(w.weather_code);
  $(".city-icon use", row).setAttribute("href", iconFor(kind, isNight()));
  $(".desc", row).textContent = w.description;
  $(".wind", row).textContent = `Vent ${Math.round(w.wind_kmh)} km/h`;
  $(".time", row).textContent = `Relevé à ${timeLabel(w.observed_at)}`;
  const badge = $(".badge", row);
  badge.textContent = FRESHNESS[w.source] || "";
  badge.hidden = !FRESHNESS[w.source];
  // style.setProperty passe par le CSSOM : compatible avec la CSP (pas de style inline)
  $(".scale-dot", row).style.setProperty("--pos", scalePosition(w.temperature_c));
  $(".city-temp", row).textContent = tempLabel(w.temperature_c);
}

function showCityError(row, message) {
  row.classList.add("failed");
  $(".desc", row).textContent = message;
  $(".wind", row).textContent = "";
  $(".time", row).textContent = "";
  $(".badge", row).hidden = true;
  $(".city-temp", row).textContent = "–";
}

async function loadCityWeather(city, row) {
  try {
    showCityWeather(row, await api(`/api/cities/${city.id}/weather`));
  } catch (err) {
    showCityError(row, err.message);
  }
}

// `item` vient de /api/weather (ville + météo) ou de POST /api/cities (ville seule)
function renderCity(item) {
  const row = template.content.firstElementChild.cloneNode(true);
  $(".name", row).textContent = item.name;
  $(".country", row).textContent = item.country ?? "";
  $(".remove", row).setAttribute("aria-label", `Retirer ${item.name} de ma liste`);
  $(".remove", row).addEventListener("click", async () => {
    try {
      await api(`/api/cities/${item.id}`, { method: "DELETE" });
      row.remove();
      updateCount();
      formMessage.className = "form-message";
      formMessage.textContent = `${item.name} retirée de votre liste.`;
    } catch (err) {
      showCityError(row, err.message);
    }
  });
  list.appendChild(row);
  if (item.weather) showCityWeather(row, item.weather);
  else if (item.error) showCityError(row, item.error);
  else loadCityWeather(item, row);
}

// Une seule requête pour toute la liste et sa météo (au lieu de 1 + N)
let lastLoad = 0;

async function loadCities() {
  lastLoad = Date.now();
  try {
    const cities = await api("/api/weather");
    list.replaceChildren();
    cities.forEach(renderCity);
    $("#empty").textContent = EMPTY_TEXT;
  } catch (err) {
    list.replaceChildren();
    $("#empty").textContent = `Impossible de charger vos villes : ${err.message}`;
  }
  updateCount();
}

// --- Ajout d'une ville, avec suggestions pendant la saisie ---------------------------------------
const input = $("#city-name");
const box = $("#suggestions");
const suggestCache = new Map(); // évite de redemander la même saisie pendant la visite
let suggestTimer = null;
let suggestController = null;
let suggestions = [];
let activeIndex = -1;
let shownQuery = "";

function normalize(text) {
  return text.trim().replace(/\s+/g, " ");
}

async function addCity(payload, label) {
  const button = $("#add-form button");
  button.disabled = true;
  formMessage.className = "form-message";
  formMessage.textContent = `Ajout de ${label}…`;
  try {
    const city = await api("/api/cities", { method: "POST", body: JSON.stringify(payload) });
    formMessage.textContent = `${city.name} ajoutée.`;
    input.value = "";
    renderCity(city);
    updateCount();
  } catch (err) {
    formMessage.className = "form-message error";
    formMessage.textContent = err.message;
  } finally {
    button.disabled = false;
  }
}

function closeSuggestions() {
  clearTimeout(suggestTimer);
  box.hidden = true;
  box.replaceChildren();
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
  suggestions = [];
  activeIndex = -1;
}

function setActive(index) {
  const options = box.querySelectorAll(".suggestion");
  options.forEach((el, i) => el.setAttribute("aria-selected", String(i === index)));
  activeIndex = index;
  if (index >= 0) {
    input.setAttribute("aria-activedescendant", options[index].id);
    options[index].scrollIntoView({ block: "nearest" });
  } else {
    input.removeAttribute("aria-activedescendant");
  }
}

function choose(index) {
  const place = suggestions[index];
  const query = shownQuery;
  closeSuggestions();
  // On renvoie la saisie d'origine et l'identifiant : le serveur retrouve ce lieu exact dans son cache
  addCity({ name: query, id: place.id }, place.name);
}

// Nom avec la partie déjà tapée en gras (construit en nœuds DOM, jamais en HTML)
function nameWithMatch(name, query) {
  const span = document.createElement("span");
  span.className = "s-name";
  const n = name.length;
  const q = query.length;
  if (q && name.slice(0, q).toLocaleLowerCase("fr") === query.toLocaleLowerCase("fr")) {
    const strong = document.createElement("strong");
    strong.textContent = name.slice(0, q);
    span.append(strong, name.slice(q, n));
  } else {
    span.textContent = name;
  }
  return span;
}

function renderSuggestions(query, results) {
  shownQuery = query;
  suggestions = results;
  activeIndex = -1;
  if (!results.length) {
    const hint = document.createElement("li");
    hint.className = "suggestion-hint";
    hint.setAttribute("role", "presentation");
    hint.textContent = query.length < 3
      ? "Tapez une lettre de plus pour voir les suggestions."
      : "Aucune ville trouvée.";
    box.replaceChildren(hint);
  } else {
    box.replaceChildren(
      ...results.map((place, i) => {
        const li = document.createElement("li");
        li.className = "suggestion";
        li.id = `suggestion-${i}`;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", "false");
        const meta = document.createElement("span");
        meta.className = "s-meta";
        meta.textContent = [...new Set([place.region, place.country].filter(Boolean))].join(", ");
        li.append(nameWithMatch(place.name, query), meta);
        // mousedown plutôt que click : le choix se fait avant que le champ ne perde le focus
        li.addEventListener("mousedown", (event) => {
          event.preventDefault();
          choose(i);
        });
        return li;
      })
    );
  }
  box.hidden = false;
  input.setAttribute("aria-expanded", "true");
}

async function fetchSuggestions(query) {
  const key = query.toLocaleLowerCase("fr");
  if (suggestCache.has(key)) return renderSuggestions(query, suggestCache.get(key));
  suggestController?.abort(); // une seule requête en vol : la frappe suivante annule la précédente
  suggestController = new AbortController();
  try {
    const results = await api(`/api/geocode?q=${encodeURIComponent(query)}`, { signal: suggestController.signal });
    suggestCache.set(key, results);
    if (normalize(input.value) === query) renderSuggestions(query, results);
  } catch (err) {
    if (err.name !== "AbortError") closeSuggestions(); // suggestions indisponibles : la saisie libre marche toujours
  }
}

input.addEventListener("input", () => {
  const query = normalize(input.value);
  clearTimeout(suggestTimer);
  if (query.length < 2) {
    suggestController?.abort();
    closeSuggestions();
    return;
  }
  // Anti-rebond : on attend une pause de frappe avant d'interroger le serveur
  suggestTimer = setTimeout(() => fetchSuggestions(query), 250);
});

input.addEventListener("keydown", (event) => {
  if (box.hidden) return;
  if (event.key === "Escape") {
    closeSuggestions();
    return;
  }
  if (!suggestions.length) return;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    setActive((activeIndex + 1) % suggestions.length);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    setActive(activeIndex <= 0 ? suggestions.length - 1 : activeIndex - 1);
  } else if (event.key === "Enter" && activeIndex >= 0) {
    event.preventDefault();
    choose(activeIndex);
  }
});

input.addEventListener("blur", () => setTimeout(closeSuggestions, 120));

$("#add-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = normalize(input.value);
  if (!name) return;
  closeSuggestions();
  addCity({ name }, name);
});

$("#refresh-btn").addEventListener("click", () => {
  loadCities();
  if (here.dataset.state === "ready") locate();
});

// --- Infos techniques (démo de scalabilité) --------------------------------------------------
// Bloc masqué pour les visiteurs ; ajouter ?tech à l'URL pour l'afficher pendant une démo.
const SHOW_TECH = new URLSearchParams(location.search).has("tech");
$("#tech").hidden = !SHOW_TECH;

$("#probe-btn").addEventListener("click", async (event) => {
  const button = event.target;
  button.disabled = true;
  const counts = new Map();
  const bump = (key) => counts.set(key, (counts.get(key) || 0) + 1);
  await Promise.all(
    Array.from({ length: 20 }, () =>
      api("/api/info")
        .then((d) => bump(`${d.instance.replica}|${d.instance.revision}`))
        .catch(() => bump("erreur|-"))
    )
  );
  const rows = [...counts.entries()].map(([key, n]) => {
    const tr = document.createElement("tr");
    for (const value of [...key.split("|"), n]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.appendChild(td);
    }
    return tr;
  });
  $("#replicas tbody").replaceChildren(...rows);
  $("#replicas").hidden = false;
  button.disabled = false;
});

async function loadBackendInfo() {
  try {
    const info = await api("/api/info");
    $("#backend-info").textContent =
      `Backend version ${info.version}, environnement ${info.environment}, réplica ${info.instance.replica}.`;
  } catch (err) {
    $("#backend-info").textContent = `Backend indisponible : ${err.message}`;
  }
}

// Retour sur l'onglet après plus de 5 minutes : on remet la météo à jour
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || Date.now() - lastLoad < 5 * 60 * 1000) return;
  loadCities();
  if (here.dataset.state === "ready") locate();
});

initHere();
loadCities();
if (SHOW_TECH) loadBackendInfo();
