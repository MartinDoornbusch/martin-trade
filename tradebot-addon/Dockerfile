# Local HA add-on. Bewust python:3.11-slim (glibc) i.p.v. HA Alpine-base:
# numpy heeft kant-en-klare aarch64 wheels voor glibc, op Alpine zou het compileren.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY config/ config/
COPY entrypoint.py .

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/app/config/config.yaml

# Geen non-root user: Supervisor beheert de container en /data mount.
CMD ["python", "entrypoint.py"]
