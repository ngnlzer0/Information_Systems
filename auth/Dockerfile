# Збірка з кореня репозиторію (context: .), щоб підтягнути спільний модуль common/
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY auth/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY auth/auth_service.py ./auth_service.py

RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app

EXPOSE 5000
# Один воркер + потоки: простий SQLite і спільний лічильник невдалих входів у пам'яті
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "auth_service:app"]
