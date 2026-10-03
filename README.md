# Elite Canva Auto

Canva email invitation backend. Runtime credentials are supplied with ELITE_CONFIG_JSON in Railway. Mount a persistent volume at /data. Owner logs in at /admin, then /canva to complete Canva email and OTP login. API requests use X-API-Key.

This service runs the web backend and browser worker. Telegram polling remains with the existing bot to avoid running two pollers on one token.
