# AI-Ready Workspace Aggregator

Backend service that collects items from corporate channels (email, Telegram,
task trackers), normalizes and **deduplicates** them into a single store, and
exposes a REST API that an AI agent queries for context.

The current build ships three connectors working end-to-end — **GitHub Issues**,
**Telegram** (polling via `getUpdates` with an offset cursor), and **Email**
(IMAP `UNSEEN` + MIME parsing with attachments uploaded to S3). A bundled
`greenmail` test server makes the email path runnable locally.

---

## Architecture

```
                 ┌────────────┐
   GitHub API ──▶│            │
   Telegram   ──▶│ Connectors │──▶ UnifiedMessage ──▶ ingest.upsert_messages
   IMAP/email ──▶│            │        (Pydantic)        (ON CONFLICT)
                 └────────────┘                              │
                                                             ▼
   Celery Beat ──▶ sync_connector (retry on 429/5xx) ──▶  PostgreSQL
                         │                                   ▲
                         ├─▶ download_attachments ─▶ MinIO (S3)
                         └─▶ embed_message ─▶ Qdrant   (optional, flag)
                                                             │
   AI agent ──▶ FastAPI  GET /api/v1/messages ◀──────────────┘
```

**Data flow:** a connector turns whatever a source returns into one canonical
`UnifiedMessage`; `ingest.upsert_messages` writes it with
`INSERT … ON CONFLICT (source_system, external_id) DO UPDATE`, so re-running a
sync over the same window updates in place instead of duplicating. Binary
attachments go to S3/MinIO; Postgres keeps only the object reference.

### Key design points (mapped to the brief)

| Requirement | Where |
|---|---|
| Unified model + relational design | [app/db/models.py](app/db/models.py), [app/schemas/message.py](app/schemas/message.py) |
| Dedup via unique index + upsert | [app/services/ingest.py](app/services/ingest.py) (`uq_messages_source_external`, `xmax = 0` insert/update detection) |
| Idempotent pipelines | re-running a sync is a no-op on data — proven in [tests/test_dedup.py](tests/test_dedup.py) |
| Retry on rate limits / 5xx | [app/tasks/sync.py](app/tasks/sync.py) + [app/connectors/github.py](app/connectors/github.py) (`RetryableError`) |
| At-least-once polling (offset advanced only after commit) | [app/connectors/telegram.py](app/connectors/telegram.py) + [app/services/cursor.py](app/services/cursor.py) |
| Scheduled background work | Celery Beat in [app/core/celery_app.py](app/core/celery_app.py) |
| S3 for files | [app/services/storage.py](app/services/storage.py); inline upload of email MIME parts in [app/connectors/email.py](app/connectors/email.py); deferred URL download in [app/tasks/attachments.py](app/tasks/attachments.py) |
| Audit / traceability | `processing_status` + `error_log` (set on permanent failures, e.g. a 404 attachment) surfaced via `GET /api/v1/messages?status=error`; `fetched_at`; structlog `trace_id` ([app/core/logging.py](app/core/logging.py)) |
| REST API for the agent | [app/api/v1/](app/api/v1/) |
| Optional semantic search | feature-flagged ([app/vector/](app/vector/), `GET /api/v1/search`) — pluggable embeddings (hash / OpenAI) + Qdrant |

---

## Stack

Python 3.11 · FastAPI · SQLAlchemy 2.0 + Alembic · PostgreSQL · Celery + Redis ·
MinIO (S3) · structlog · httpx · Docker Compose · (optional) Qdrant.

Sync SQLAlchemy is used in both the API and the workers so there is no
async/Celery friction; FastAPI runs the read endpoints in a threadpool.

---

## Quick start

```bash
cp .env.example .env
make up            # build + start api, worker, beat, postgres, redis, minio
```

The `api` service runs `alembic upgrade head` on boot. Then:

```bash
# Force a GitHub sync (defaults to the pydantic/pydantic repo; set GITHUB_REPOS/GITHUB_TOKEN in .env)
make sync
# or:
curl -X POST http://localhost:8000/api/v1/connectors/github/sync

# Read what the agent would read
curl "http://localhost:8000/api/v1/messages?source=github&limit=10"
```

- API docs (Swagger): http://localhost:8000/docs
- MinIO console: http://localhost:9001 (`minioadmin` / `minioadmin`)

### Optional semantic search (vector module)

```bash
docker compose --profile vector up -d qdrant
# in .env: VECTOR_ENABLED=true  (EMBEDDING_PROVIDER defaults to "hash" — no key needed)
docker compose up -d --force-recreate api worker beat

make sync                                        # ingest + embed into Qdrant
curl "http://localhost:8000/api/v1/search?q=deploy%20failed&limit=5"
```

Embeddings are pluggable via `EMBEDDING_PROVIDER`:
- `hash` (default) — deterministic, dependency-free, no key. Runs anywhere; it is
  hashed bag-of-words, **not** a true semantic model — for plumbing/demo.
- `openai` — real semantic embeddings (`text-embedding-3-small`); set `OPENAI_API_KEY`.

Switching providers changes the vector dimension, so recreate the Qdrant
collection (drop it, or change `QDRANT_COLLECTION`) when you switch.

---

## API

| Method | Path | Description |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/api/v1/messages` | list with `source`, `status`, `q`, `limit`, `offset` |
| GET | `/api/v1/messages/{id}` | single message + attachments |
| GET | `/api/v1/connectors` | connectors and whether they are configured |
| POST | `/api/v1/connectors/{source}/sync` | enqueue a sync, returns Celery `task_id` |
| GET | `/api/v1/search` | semantic search (`q`, `limit`); needs `VECTOR_ENABLED=true` |

---

## Tests

```bash
make test          # docker compose run --rm api pytest
```

Tests run against the real Postgres because the idempotency guarantee depends on
`ON CONFLICT` and the `xmax = 0` trick. They use a dedicated `aggregator_test`
database (created automatically) so development data is never touched. The
headline test asserts that replaying a sync produces zero new rows.

```bash
make lint          # ruff
```

---

## Project layout

```
app/
  core/        config, structlog logging (trace_id), celery app
  db/          Base, session, ORM models (Message, Attachment)
  schemas/     UnifiedMessage + API read models
  connectors/  base ABC, github + telegram + email (full), registry
  services/    ingest (idempotent upsert), storage (S3/MinIO)
  tasks/       sync (retry/backoff), attachments, embeddings (flagged)
  api/v1/      messages, connectors routers
alembic/       migrations
tests/         dedup idempotency + API
```

---

## Roadmap

- [x] Telegram connector — polling via `getUpdates` with an offset cursor (Redis)
- [x] Email connector — IMAP `UNSEEN` (PEEK) + MIME parse + attachments → S3
- [x] Semantic search — pluggable embeddings (hash / OpenAI) + Qdrant + `/search`
- [x] Incremental GitHub sync via `since` + per-repo cursor (advanced post-commit)
- [x] Per-message `error_log` + `processing_status=error`, surfaced through the API
- [ ] Telegram attachments — resolve `getFile` download path and push to S3
- [ ] Claude-powered enrichment — summaries / classification (`claude-opus-4-8`)
```
