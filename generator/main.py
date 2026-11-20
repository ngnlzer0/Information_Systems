"""Генератор навантаження для PostgreSQL.

Постійно виконує випадкові операції (вставка замовлень, оновлення статусів, звіти,
повільні запити та навмисні помилки), щоб у БД завжди були "живі" дані, а в
Prometheus / Loki / Tempo - метрики, логи та трейси.

Метрики цього сервісу доступні на :8001/metrics.
"""
import os
import random
import time

import psycopg2
from opentelemetry import trace
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import Counter, Histogram, start_http_server

from common.obs import setup_logging, setup_tracing

SERVICE = "generator"

log = setup_logging(SERVICE)
setup_tracing(SERVICE)
Psycopg2Instrumentor().instrument()  # кожен SQL-запит стає окремим спаном у трейсі
tracer = trace.get_tracer(SERVICE)

DB = {
    "host": os.getenv("DB_HOST", "postgres"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "shop"),
    "user": os.getenv("DB_USER", "app_user"),
    "password": os.getenv("DB_PASSWORD", ""),
}
INTERVAL = float(os.getenv("GEN_INTERVAL_SECONDS", "0.5"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "8001"))

# ---------------------------------------------------------------- метрики
OPS = Counter("db_operations", "Кількість операцій з БД", ["operation", "status"])
LATENCY = Histogram(
    "db_operation_duration_seconds",
    "Тривалість операції з БД (секунди)",
    ["operation"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

# ---------------------------------------------------------- довідкові дані
FIRST_NAMES = [
    ("Олена", "olena"), ("Андрій", "andrii"), ("Марія", "maria"), ("Іван", "ivan"),
    ("Софія", "sofia"), ("Богдан", "bohdan"), ("Наталія", "natalia"), ("Тарас", "taras"),
]
LAST_NAMES = [
    ("Шевченко", "shevchenko"), ("Коваленко", "kovalenko"), ("Бондаренко", "bondarenko"),
    ("Мельник", "melnyk"), ("Ткаченко", "tkachenko"), ("Кравченко", "kravchenko"),
]


class State:
    """Кеш ідентифікаторів, щоб не ходити в БД за ними перед кожною операцією."""

    def __init__(self):
        self.customers = []
        self.products = {}  # id -> ціна


def load_reference_data(conn):
    state = State()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM customers")
        if cur.fetchone()[0] == 0:
            for i in range(1, 51):
                first, first_lat = random.choice(FIRST_NAMES)
                last, last_lat = random.choice(LAST_NAMES)
                cur.execute(
                    "INSERT INTO customers (name, email) VALUES (%s, %s)",
                    (f"{first} {last}", f"{first_lat}.{last_lat}{i}@example.com"),
                )
            log.info("Створено початкових клієнтів", extra={"rows": 50})
        cur.execute("SELECT id FROM customers")
        state.customers = [row[0] for row in cur.fetchall()]
        cur.execute("SELECT id, price FROM products")
        state.products = {row[0]: row[1] for row in cur.fetchall()}
    conn.commit()
    return state


# ---------------------------------------------------------------- операції
def op_insert_order(cur, st):
    customer_id = random.choice(st.customers)
    product_id = random.choice(list(st.products))
    quantity = random.randint(1, 5)
    total = st.products[product_id] * quantity
    cur.execute(
        "INSERT INTO orders (customer_id, product_id, quantity, total) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (customer_id, product_id, quantity, total),
    )
    order_id = cur.fetchone()[0]
    cur.execute("UPDATE products SET stock = stock - %s WHERE id = %s", (quantity, product_id))
    return {"order_id": order_id, "quantity": quantity}


TRANSITIONS = [("new", "paid"), ("paid", "shipped"), ("new", "cancelled")]


def op_update_status(cur, st):
    old_status, new_status = random.choice(TRANSITIONS)
    cur.execute(
        "UPDATE orders SET status = %s, updated_at = now() "
        "WHERE id = (SELECT id FROM orders WHERE status = %s ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
        "RETURNING id",
        (new_status, old_status),
    )
    row = cur.fetchone()
    return {"order_id": row[0] if row else None, "new_status": new_status}


def op_report(cur, st):
    cur.execute(
        "SELECT p.name, count(*), sum(o.total) "
        "FROM orders o JOIN products p ON p.id = o.product_id "
        "WHERE o.created_at > now() - interval '10 minutes' "
        "GROUP BY p.name ORDER BY 3 DESC"
    )
    return {"rows": len(cur.fetchall())}


def op_customer_history(cur, st):
    cur.execute(
        "SELECT id, status, total FROM orders WHERE customer_id = %s ORDER BY created_at DESC LIMIT 20",
        (random.choice(st.customers),),
    )
    return {"rows": len(cur.fetchall())}


def op_restock(cur, st):
    cur.execute("UPDATE products SET stock = stock + 200 WHERE id = %s", (random.choice(list(st.products)),))
    return {}


def op_new_customer(cur, st):
    first, first_lat = random.choice(FIRST_NAMES)
    last, last_lat = random.choice(LAST_NAMES)
    suffix = random.randint(100_000, 999_999)
    cur.execute(
        "INSERT INTO customers (name, email) VALUES (%s, %s) RETURNING id",
        (f"{first} {last}", f"{first_lat}.{last_lat}{suffix}@example.com"),
    )
    return {"new_customer_id": cur.fetchone()[0]}


def op_slow_query(cur, st):
    seconds = round(random.uniform(0.4, 1.5), 2)
    cur.execute("SELECT pg_sleep(%s)", (seconds,))
    return {"sleep_seconds": seconds}


def op_bad_insert(cur, st):
    # Навмисна помилка: quantity = -1 порушує CHECK (quantity > 0)
    cur.execute(
        "INSERT INTO orders (customer_id, product_id, quantity, total) VALUES (%s, %s, -1, 0)",
        (random.choice(st.customers), random.choice(list(st.products))),
    )
    return {}


# назва -> (функція, вага: чим більша, тим частіше виконується)
OPERATIONS = {
    "insert_order": (op_insert_order, 40),
    "update_status": (op_update_status, 20),
    "customer_history": (op_customer_history, 15),
    "report": (op_report, 10),
    "restock": (op_restock, 5),
    "new_customer": (op_new_customer, 3),
    "slow_query": (op_slow_query, 4),
    "bad_insert": (op_bad_insert, 3),
}


# ------------------------------------------------------------------ ядро
def connect():
    while True:
        try:
            conn = psycopg2.connect(connect_timeout=5, application_name=SERVICE, **DB)
            log.info("Підключено до БД", extra={"db_host": DB["host"], "db_name": DB["dbname"]})
            return conn
        except psycopg2.OperationalError as exc:
            log.warning("БД недоступна, повтор через 3 с: %s", str(exc).strip())
            time.sleep(3)


def safe_rollback(conn):
    try:
        conn.rollback()
    except Exception:
        pass  # з'єднання втрачено - головний цикл перепідключиться


def run_operation(conn, state, name, fn):
    status = "ok"
    started = time.perf_counter()
    with tracer.start_as_current_span(f"db.{name}") as span:
        span.set_attribute("app.operation", name)
        try:
            with conn.cursor() as cur:
                details = fn(cur, state) or {}
            conn.commit()
            if "new_customer_id" in details:
                state.customers.append(details["new_customer_id"])
            log.info("Операцію виконано", extra={"operation": name, **details})
        except Exception as exc:
            status = "error"
            safe_rollback(conn)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            first_line = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            log.error("Помилка операції: %s", first_line, extra={"operation": name})
    OPS.labels(name, status).inc()
    LATENCY.labels(name).observe(time.perf_counter() - started)


def main():
    start_http_server(METRICS_PORT)
    conn = connect()
    state = load_reference_data(conn)
    names = list(OPERATIONS)
    weights = [OPERATIONS[n][1] for n in names]
    log.info("Генератор запущено", extra={"interval_seconds": INTERVAL, "metrics_port": METRICS_PORT})

    while True:
        if conn.closed:
            conn = connect()
        name = random.choices(names, weights)[0]
        run_operation(conn, state, name, OPERATIONS[name][0])
        time.sleep(random.uniform(0.5, 1.5) * INTERVAL)


if __name__ == "__main__":
    main()
