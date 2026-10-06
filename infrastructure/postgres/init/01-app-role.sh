#!/bin/bash
# Runs once, when the data volume is first initialised.
# Creates the restricted roles the API and the worker connect as. Neither owns anything or can
# bypass RLS; migrations (run as the owner) grant each exactly the privileges it needs.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_password="$APP_DB_PASSWORD" \
     -v worker_password="$WORKER_DB_PASSWORD" <<'EOSQL'
CREATE ROLE docunexus_app LOGIN PASSWORD :'app_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE docunexus_worker LOGIN PASSWORD :'worker_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT CONNECT ON DATABASE docunexus TO docunexus_app, docunexus_worker;
EOSQL
