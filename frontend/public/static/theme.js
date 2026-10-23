// Thème clair / sombre. Chargé dans <head>, avant l'affichage, pour éviter un flash de la mauvaise couleur.
// Fichier séparé et non inline : la CSP (default-src 'self') interdit les scripts inline.
// Sans choix enregistré, le thème suit le réglage du système (CSS prefers-color-scheme).
(function () {
  "use strict";
  var KEY = "meteo-theme";
  var root = document.documentElement;
  var systemDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function saved() {
    try {
      var v = localStorage.getItem(KEY);
      return v === "light" || v === "dark" ? v : null;
    } catch (e) {
      return null; // navigation privée stricte, stockage bloqué : on suit le système
    }
  }

  function current() {
    return root.dataset.theme || (systemDark && systemDark.matches ? "dark" : "light");
  }

  var choice = saved();
  if (choice) root.dataset.theme = choice;

  function syncButton() {
    var btn = document.getElementById("theme-btn");
    if (!btn) return;
    var dark = current() === "dark";
    // L'icône montre le mode vers lequel on bascule
    btn.querySelector("use").setAttribute("href", dark ? "#i-sun" : "#i-moon");
    var label = dark ? "Passer en mode clair" : "Passer en mode sombre";
    btn.setAttribute("aria-label", label);
    btn.title = label;
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-btn");
    if (!btn) return;
    syncButton();
    btn.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try { localStorage.setItem(KEY, next); } catch (e) { /* choix gardé pour cette page seulement */ }
      syncButton();
    });
  });

  // Le système change de thème (ex. passage automatique le soir) et aucun choix n'est enregistré
  if (systemDark && systemDark.addEventListener) {
    systemDark.addEventListener("change", syncButton);
  }
})();
