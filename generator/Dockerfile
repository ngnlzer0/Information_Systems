# Збірка з кореня репозиторію (context: .), щоб підтягнути спільний модуль common/
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY generator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY generator/main.py ./main.py

RUN useradd --system --uid 10001 app
USER app

EXPOSE 8001
CMD ["python", "main.py"]
