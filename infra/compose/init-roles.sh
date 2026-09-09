#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=demo_password="$DEMO_PASSWORD" \
  --set=investigation_password="$INVESTIGATION_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE demo LOGIN PASSWORD %L', :'demo_password') \gexec
SELECT format('CREATE ROLE investigation LOGIN PASSWORD %L', :'investigation_password') \gexec
ALTER ROLE investigation SET default_transaction_read_only = on;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
