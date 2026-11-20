"""Prometheus-експортер метрик PostgreSQL.

Під час кожного скрейпу Prometheus (/metrics, порт 8000) експортер підключається
до БД від імені ролі monitor_user (pg_monitor), читає системні view статистики
і віддає їх у форматі Prometheus.

Джерела: pg_stat_database, pg_stat_activity, pg_stat_user_tables, pg_stat_statements.
"""
import os
import threading

import psycopg2
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from common.obs import setup_logging

SERVICE = "db-exporter"
log = setup_logging(SERVICE)

DSN = {
    "host": os.getenv("DB_HOST", "postgres"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "shop"),
    "user": os.getenv("DB_USER", "monitor_user"),
    "password": os.getenv("DB_PASSWORD", ""),
    "connect_timeout": 3,
    "application_name": SERVICE,
}
PORT = int(os.getenv("EXPORTER_PORT", "8000"))

# Колонки pg_stat_database, які віддаємо як лічильники (значення лише зростають)
DB_COUNTERS = {
    "xact_commit": "Підтверджені транзакції",
    "xact_rollback": "Відкочені транзакції",
    "blks_read": "Блоки, прочитані з диска",
    "blks_hit": "Блоки, знайдені в буферному кеші",
    "tup_returned": "Рядки, повернуті запитами (скан)",
    "tup_fetched": "Рядки, отримані запитами",
    "tup_inserted": "Вставлені рядки",
    "tup_updated": "Оновлені рядки",
    "tup_deleted": "Видалені рядки",
    "deadlocks": "Виявлені взаємоблокування",
    "temp_files": "Створені тимчасові файли",
    "temp_bytes": "Байти у тимчасових файлах",
}


class PostgresCollector:
    def __init__(self):
        self._last_error = None
        self._statements_warned = False

    # Prometheus викликає collect() на кожен скрейп
    def collect(self):
        families = []
        up = 0
        try:
            conn = psycopg2.connect(**DSN)
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    families += self._databases(cur)
                    families += self._activity(cur)
                    families += self._tables(cur)
                    families += self._statements(cur)
                up = 1
            finally:
                conn.close()
            if self._last_error:
                log.info("Зв'язок з PostgreSQL відновлено")
                self._last_error = None
        except Exception as exc:
            message = str(exc).strip()
            if message != self._last_error:  # не спамимо однаковою помилкою кожні 5 с
                log.error("Не вдалося зібрати метрики PostgreSQL: %s", message)
                self._last_error = message
        families.append(GaugeMetricFamily("pg_up", "1, якщо експортер підключився до PostgreSQL", value=up))
        return families

    # ----------------------------------------------------- pg_stat_database
    def _databases(self, cur):
        columns = ", ".join(DB_COUNTERS)
        cur.execute(
            "SELECT datname, numbackends, pg_database_size(datname), " + columns +
            " FROM pg_stat_database WHERE datname IS NOT NULL AND datname NOT LIKE 'template%'"
        )
        rows = cur.fetchall()

        backends = GaugeMetricFamily("pg_stat_database_numbackends", "Активні підключення до БД", labels=["datname"])
        size = GaugeMetricFamily("pg_database_size_bytes", "Розмір БД, байти", labels=["datname"])
        counters = {
            col: CounterMetricFamily(f"pg_stat_database_{col}", doc, labels=["datname"])
            for col, doc in DB_COUNTERS.items()
        }
        for row in rows:
            datname = row[0]
            backends.add_metric([datname], row[1] or 0)
            size.add_metric([datname], row[2] or 0)
            for col, value in zip(DB_COUNTERS, row[3:]):
                counters[col].add_metric([datname], value or 0)
        return [backends, size, *counters.values()]

    # ---------------------------------------------------- pg_stat_activity
    def _activity(self, cur):
        cur.execute(
            "SELECT COALESCE(state, 'unknown'), count(*) FROM pg_stat_activity "
            "WHERE datname IS NOT NULL GROUP BY 1"
        )
        connections = GaugeMetricFamily(
            "pg_stat_activity_connections", "Підключення за станом", labels=["state"]
        )
        for state, count in cur.fetchall():
            connections.add_metric([state], count)

        cur.execute(
            "SELECT COALESCE(max(EXTRACT(EPOCH FROM (now() - query_start))), 0) "
            "FROM pg_stat_activity WHERE state = 'active' AND pid <> pg_backend_pid()"
        )
        longest = GaugeMetricFamily(
            "pg_stat_activity_longest_query_seconds", "Тривалість найдовшого активного запиту, секунди"
        )
        longest.add_metric([], float(cur.fetchone()[0]))

        cur.execute("SELECT setting::int FROM pg_settings WHERE name = 'max_connections'")
        max_conn = GaugeMetricFamily("pg_settings_max_connections", "Параметр max_connections")
        max_conn.add_metric([], cur.fetchone()[0])
        return [connections, longest, max_conn]

    # -------------------------------------------------- pg_stat_user_tables
    def _tables(self, cur):
        cur.execute(
            "SELECT relname, n_live_tup, n_dead_tup, seq_scan, idx_scan, pg_total_relation_size(relid) "
            "FROM pg_stat_user_tables"
        )
        live = GaugeMetricFamily("pg_table_live_tuples", "Приблизна кількість живих рядків", labels=["table"])
        dead = GaugeMetricFamily("pg_table_dead_tuples", "Приблизна кількість мертвих рядків", labels=["table"])
        size = GaugeMetricFamily("pg_table_size_bytes", "Розмір таблиці з індексами, байти", labels=["table"])
        seq = CounterMetricFamily("pg_table_seq_scan", "Послідовні скани таблиці", labels=["table"])
        idx = CounterMetricFamily("pg_table_idx_scan", "Скани за індексом", labels=["table"])
        for relname, n_live, n_dead, seq_scan, idx_scan, total_size in cur.fetchall():
            live.add_metric([relname], n_live or 0)
            dead.add_metric([relname], n_dead or 0)
            size.add_metric([relname], total_size or 0)
            seq.add_metric([relname], seq_scan or 0)
            idx.add_metric([relname], idx_scan or 0)
        return [live, dead, size, seq, idx]

    # ----------------------------------------------------- pg_stat_statements
    def _statements(self, cur):
        """Топ-10 запитів за сумарним часом. Обмеження top-N тримає кардинальність під контролем."""
        try:
            cur.execute(
                "SELECT queryid::text, left(regexp_replace(query, '\\s+', ' ', 'g'), 80), "
                "       calls, total_exec_time, rows "
                "FROM pg_stat_statements "
                "WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "ORDER BY total_exec_time DESC LIMIT 10"
            )
            rows = cur.fetchall()
        except psycopg2.Error as exc:
            if not self._statements_warned:
                log.warning("pg_stat_statements недоступний, метрики запитів пропущено: %s", str(exc).strip())
                self._statements_warned = True
            return []

        labels = ["queryid", "query"]
        calls = CounterMetricFamily("pg_stat_statements_calls", "Кількість викликів запиту", labels=labels)
        seconds = CounterMetricFamily(
            "pg_stat_statements_exec_time_seconds", "Сумарний час виконання запиту, секунди", labels=labels
        )
        affected = CounterMetricFamily("pg_stat_statements_rows", "Рядки, повернуті/змінені запитом", labels=labels)
        for queryid, query, n_calls, total_ms, n_rows in rows:
            label_values = [queryid, query]
            calls.add_metric(label_values, n_calls)
            seconds.add_metric(label_values, total_ms / 1000.0)
            affected.add_metric(label_values, n_rows)
        return [calls, seconds, affected]


def main():
    REGISTRY.register(PostgresCollector())
    start_http_server(PORT)
    log.info("Експортер запущено", extra={"port": PORT})
    threading.Event().wait()


if __name__ == "__main__":
    main()
