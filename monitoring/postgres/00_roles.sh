#!/bin/bash
# First start of the reporting database: the two logins (passwords from .env), then the tables (init.sql).
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v writer_pw="$REPORTING_WRITER_PASSWORD" -v reader_pw="$REPORTING_READER_PASSWORD" <<'SQL'
CREATE ROLE nbo_writer LOGIN PASSWORD :'writer_pw';
CREATE ROLE grafana_reader LOGIN PASSWORD :'reader_pw';
GRANT CONNECT, CREATE ON DATABASE reporting TO nbo_writer;
GRANT CONNECT ON DATABASE reporting TO grafana_reader;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /docker-entrypoint-initdb.d/sql/init.sql