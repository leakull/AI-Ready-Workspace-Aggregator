# syntax=docker/dockerfile:1
# --------------------------------------------------------------------------- #
# base: production dependencies only
# --------------------------------------------------------------------------- #
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /code

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

# --------------------------------------------------------------------------- #
# dev: adds test/lint tooling. Runs as root so it can write to the mounted
# source volume (.pytest_cache, __pycache__). Used by docker-compose locally.
# --------------------------------------------------------------------------- #
FROM base AS dev
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# --------------------------------------------------------------------------- #
# runtime: lean, non-root production image
# --------------------------------------------------------------------------- #
FROM base AS runtime
COPY . .
RUN useradd --create-home appuser && chown -R appuser:appuser /code
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
