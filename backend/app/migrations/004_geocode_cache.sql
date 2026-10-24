-- Cache des suggestions de villes (géocodage), partagé par tous les réplicas.
-- Sans lui, chaque nouveau réplica (scale-out) repart d'un cache mémoire vide et rappelle Open-Meteo
-- pour les mêmes textes : sous charge, ces rafales d'appels déclenchent les limites de l'API (429).
CREATE TABLE IF NOT EXISTS geocode_cache (
    query      TEXT PRIMARY KEY,          -- texte saisi normalisé (espaces réduits, minuscules)
    results    JSONB NOT NULL,            -- liste des résultats (peut être vide : « aucune ville »)
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
