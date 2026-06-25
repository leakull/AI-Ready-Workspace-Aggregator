# Use the compose v2 subcommand when available, else the standalone binary.
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")

.PHONY: help up down logs build migrate revision sync demo shell test lint fmt

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Build & start the full stack (api, worker, beat, postgres, redis, minio)
	$(COMPOSE) up --build -d

down: ## Stop the stack and remove volumes
	$(COMPOSE) down -v

logs: ## Tail logs from all services
	$(COMPOSE) logs -f

build: ## Rebuild images
	$(COMPOSE) build

migrate: ## Apply DB migrations
	$(COMPOSE) run --rm api alembic upgrade head

revision: ## Autogenerate a new migration: make revision m="message"
	$(COMPOSE) run --rm api alembic revision --autogenerate -m "$(m)"

sync: ## Trigger a GitHub sync via the API
	curl -fsS -X POST http://localhost:8000/api/v1/connectors/github/sync | python3 -m json.tool

demo: up ## One command: start the stack, run a GitHub sync, show ingested messages
	@echo "Waiting for the API..."; \
	until curl -fsS http://localhost:8000/health >/dev/null 2>&1; do sleep 2; done; \
	echo "Triggering GitHub sync:"; \
	curl -fsS -X POST http://localhost:8000/api/v1/connectors/github/sync | python3 -m json.tool; \
	echo "Ingesting (give the worker a few seconds)..."; sleep 8; \
	echo "Sample of ingested messages:"; \
	curl -fsS "http://localhost:8000/api/v1/messages?source=github&limit=3" | python3 -m json.tool; \
	echo "Swagger UI: http://localhost:8000/docs"

shell: ## Open a shell in the api container
	$(COMPOSE) run --rm api /bin/sh

test: ## Run the test suite (isolated test database)
	$(COMPOSE) run --rm -e POSTGRES_DB=aggregator_test api pytest

lint: ## Lint with ruff
	$(COMPOSE) run --rm api ruff check app tests

fmt: ## Auto-format with ruff
	$(COMPOSE) run --rm api ruff check --fix app tests
