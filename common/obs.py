"""Спільні засоби спостережуваності для всіх Python-сервісів.

* setup_logging() - JSON-логи у stdout і одночасно надсилання їх у Grafana Loki;
* setup_tracing() - налаштування OpenTelemetry і експорт трейсів у Grafana Tempo.

Помилки зв'язку з Loki/Tempo ніколи не повинні ламати сам сервіс, тому всі
мережеві виклики тут "тихі": у гіршому випадку частина логів буде втрачена.
"""
import json
import logging
import os
import queue
import sys
import threading
import time

import requests

LOKI_URL = os.getenv("LOKI_URL", "")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

# Стандартні поля LogRecord - усе інше, передане через extra={...}, потрапить у JSON.
_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def _current_trace_id():
    """trace_id активного спану (hex) або None, якщо трейсинг не активний."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except ImportError:
        pass
    return None


class JsonFormatter(logging.Formatter):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def format(self, record):
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        trace_id = _current_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class LokiHandler(logging.Handler):
    """Збирає записи у чергу і пачками надсилає в Loki з фонового потоку."""

    def __init__(self, service, url, batch_size=50, flush_interval=2.0):
        super().__init__()
        self.service = service
        self.push_url = url.rstrip("/") + "/loki/api/v1/push"
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._queue = queue.Queue(maxsize=10_000)
        threading.Thread(target=self._worker, name="loki-pusher", daemon=True).start()

    def emit(self, record):
        try:
            line = self.format(record)
            self._queue.put_nowait((str(time.time_ns()), record.levelname.lower(), line))
        except Exception:  # черга переповнена або помилка форматування - просто пропускаємо
            pass

    def _worker(self):
        session = requests.Session()
        buffer = []
        last_flush = time.monotonic()
        while True:
            try:
                buffer.append(self._queue.get(timeout=0.5))
            except queue.Empty:
                pass
            due = time.monotonic() - last_flush >= self.flush_interval
            if buffer and (len(buffer) >= self.batch_size or due):
                self._push(session, buffer)
                buffer = []
                last_flush = time.monotonic()
            elif not buffer:
                last_flush = time.monotonic()

    def _push(self, session, items):
        streams = {}
        for ts, level, line in items:
            streams.setdefault(level, []).append([ts, line])
        body = {
            "streams": [
                {"stream": {"service": self.service, "level": level}, "values": values}
                for level, values in streams.items()
            ]
        }
        try:
            session.post(self.push_url, json=body, timeout=3)
        except Exception:
            pass  # Loki ще не піднявся або недоступний


def setup_logging(service):
    """Налаштовує кореневий логер: JSON у stdout (+ Loki, якщо задано LOKI_URL)."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

    formatter = JsonFormatter(service)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    if LOKI_URL:
        loki_handler = LokiHandler(service, LOKI_URL)
        loki_handler.setFormatter(formatter)
        root.addHandler(loki_handler)

    # Бібліотеки, які самі ходять по мережі, не повинні генерувати логи про наші ж запити в Loki
    for noisy in ("urllib3", "requests", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(service)


def setup_tracing(service):
    """Налаштовує OpenTelemetry з експортом у Tempo. Без OTLP-адреси трейсинг вимкнений."""
    if not OTLP_ENDPOINT:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return None

    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT.rstrip("/") + "/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    return provider
