-- Bail de rafraîchissement du cache : évite qu'à l'expiration du TTL, toutes les requêtes
-- concurrentes (tous réplicas confondus) appellent l'API météo en même temps.
ALTER TABLE weather_cache ADD COLUMN refresh_lease_until TIMESTAMPTZ;
