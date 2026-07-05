# Multi-arch (linux/amd64 + linux/arm64 voor Raspberry Pi)
FROM python:3.11-slim AS base

RUN groupadd -r bot && useradd -r -g bot bot
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY config/ config/

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/app/config/config.yaml

RUN mkdir -p /app/data && chown -R bot:bot /app/data
USER bot

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import httpx; httpx.get('http://localhost:8000/healthz').raise_for_status()"

CMD ["python", "-m", "tradebot.main"]
