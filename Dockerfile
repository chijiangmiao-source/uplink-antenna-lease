FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Production image: API service only.
FROM base AS api
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x entrypoint.sh
EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]

# Acceptance image: API code + pytest tooling, used by the one-shot
# "verify" service which runs the real concurrency suite.
FROM base AS verify
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY tests ./tests
COPY pytest.ini ./pytest.ini
CMD ["pytest"]
