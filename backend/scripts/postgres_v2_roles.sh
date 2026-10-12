#!/bin/sh
# Runs only during initialization of this Compose stack's empty PG volume.
set -eu
: "${POSTGRES_USER:?migration role is required}"
: "${POSTGRES_DB:?database name is required}"
: "${ZHIDA_V2_APP_DATABASE_PASSWORD:?runtime password is required}"

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
  --set runtime_password="$ZHIDA_V2_APP_DATABASE_PASSWORD" \
  --set migration_role="$POSTGRES_USER" --set database_name="$POSTGRES_DB" <<'SQL'
CREATE ROLE zhida_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD :'runtime_password';
GRANT CONNECT ON DATABASE :"database_name" TO zhida_runtime;
CREATE SCHEMA IF NOT EXISTS zhida;
CREATE SCHEMA IF NOT EXISTS rag;
CREATE SCHEMA IF NOT EXISTS workbench;
GRANT USAGE ON SCHEMA zhida, rag, workbench, public TO zhida_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA zhida, rag, workbench
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO zhida_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA zhida, rag, workbench
  GRANT USAGE, SELECT ON SEQUENCES TO zhida_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA zhida, rag, workbench TO zhida_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA zhida, rag, workbench TO zhida_runtime;
SQL
