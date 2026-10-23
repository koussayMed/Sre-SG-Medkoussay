-- Villes suivies. external_id = identifiant Open-Meteo (geonames) : empêche les doublons.
CREATE TABLE cities (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    external_id BIGINT           NOT NULL UNIQUE,
    name        TEXT             NOT NULL,
    country     TEXT,
    latitude    DOUBLE PRECISION NOT NULL,
    longitude   DOUBLE PRECISION NOT NULL,
    created_at  TIMESTAMPTZ      NOT NULL DEFAULT now()
);

-- Dernière météo connue par ville : cache (TTL) et repli en mode dégradé.
CREATE TABLE weather_cache (
    city_id       BIGINT PRIMARY KEY REFERENCES cities (id) ON DELETE CASCADE,
    temperature_c DOUBLE PRECISION NOT NULL,
    wind_kmh      DOUBLE PRECISION NOT NULL,
    weather_code  INTEGER,
    observed_at   TIMESTAMPTZ      NOT NULL,
    fetched_at    TIMESTAMPTZ      NOT NULL
);
