#!/bin/bash
# Runs once, when the data volume is first initialised.
# Creates the restricted role the API connects as. It owns nothing and cannot bypass RLS;
# migrations (run as the owner) grant it exactly the table privileges it needs.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_password="$APP_DB_PASSWORD" <<'EOSQL'
CREATE ROLE docunexus_app LOGIN PASSWORD :'app_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT CONNECT ON DATABASE docunexus TO docunexus_app;
EOSQL
