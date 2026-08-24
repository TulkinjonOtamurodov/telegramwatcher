FROM python:3.12-slim

# Unbuffered output so `docker compose logs -f` is live; UTC everywhere so the
# timestamps in alerts match the ones in the log file.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

WORKDIR /app

# Dependencies first: this layer is cached across code-only rebuilds.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data

# These are bind-mounted by docker-compose; creating them keeps `docker run`
# without compose working too.
RUN mkdir -p /app/sessions /app/logs /app/data

# Fast, offline check: files present, directories writable, session in place.
HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD python -m app.health || exit 1

CMD ["python", "-m", "app.main"]
