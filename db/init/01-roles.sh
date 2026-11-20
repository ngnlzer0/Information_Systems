#!/bin/bash
# Виконується один раз при першому створенні БД (docker-entrypoint-initdb.d).
# Створює дві ролі з мінімально потрібними правами:
#   app_user      - працює з даними (генератор);
#   monitor_user  - лише читає статистику (pg_monitor), для експортера метрик.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE app_user LOGIN PASSWORD '${APP_DB_PASSWORD}';
    CREATE ROLE monitor_user LOGIN PASSWORD '${MONITOR_DB_PASSWORD}';
    GRANT pg_monitor TO monitor_user;

    -- Статистика запитів (потребує shared_preload_libraries у docker-compose.yml)
    CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
EOSQL
