.PHONY: requirements install lint format typecheck test check fetch ingest ingest-dry ingest-status inspect check3 serve smoke \
	eval-validate eval-retrieval eval-answers eval-report eval-fixture eval-gate load \
	images up up-observability up-gpu up-laptop down ps logs docker-ingest docker-status docker-eval \
	backup restore deploy rollback smoke-release

BACKEND := backend

install:
	cd $(BACKEND) && uv sync --extra rerank

lint:
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .

format:
	cd $(BACKEND) && uv run ruff check --fix . && uv run ruff format .

typecheck:
	cd $(BACKEND) && uv run mypy

test:
	cd $(BACKEND) && uv run pytest

check: lint typecheck test

fetch:
	cd $(BACKEND) && uv run lexai-ingest fetch

ingest:
	cd $(BACKEND) && uv run lexai-ingest build

ingest-dry:
	cd $(BACKEND) && uv run lexai-ingest build --dry-run

ingest-status:
	cd $(BACKEND) && uv run lexai-ingest status

inspect:
	cd $(BACKEND) && uv run python scripts/inspect_index.py

serve:
	cd $(BACKEND) && uv run uvicorn lexai.api.app:create_app --factory --port 8000 --reload

smoke:
	cd $(BACKEND) && uv run python scripts/smoke_phase1.py

check3:
	cd $(BACKEND) && uv run python scripts/check_phase3.py

eval-validate:
	cd $(BACKEND) && uv run python -m lexai.evaluation validate

eval-retrieval:
	cd $(BACKEND) && uv run python -m lexai.evaluation retrieval --configs hybrid,dense,bm25,no-routing

eval-answers:
	cd $(BACKEND) && uv run python -m lexai.evaluation answers --limit 40

eval-report:
	cd $(BACKEND) && uv run python -m lexai.evaluation report

eval-fixture:
	cd $(BACKEND) && uv run python -m lexai.evaluation export-fixture

eval-gate:
	cd $(BACKEND) && uv run python -m lexai.evaluation offline

load:
	cd $(BACKEND) && uv run python scripts/load_test.py

# Regenerate the pip requirement files (used on Windows without uv) from uv.lock.
requirements:
	cd $(BACKEND) && H="# Generated from uv.lock - do not edit. Regenerate: make requirements" && \
	  { echo "$$H"; uv export --no-hashes --no-emit-project --no-dev --frozen -q | grep -v '^ *#'; } > requirements.txt && \
	  { echo "$$H"; echo "-r requirements.txt"; uv export --no-hashes --no-emit-project --only-group dev --frozen -q | grep -v '^ *#'; } > requirements-dev.txt && \
	  uv export --no-hashes --no-emit-project --no-dev --extra rerank --frozen -q | grep -v '^ *#' > .rerank.tmp && \
	  { echo "$$H"; echo "# Optional: in-process reranker (pulls in torch, ~2.5 GB RAM when loaded)."; echo "-r requirements.txt"; \
	    grep -vxFf requirements.txt .rerank.tmp; } > requirements-rerank.txt && rm -f .rerank.tmp

# ---- Docker Compose (deploy/; operations: docs/runbook.md) -------------------------------
COMPOSE := docker compose --project-directory deploy -f deploy/compose.yaml
LAPTOP := docker compose --project-directory deploy --env-file deploy/env/laptop-docker.env \
	-f deploy/compose.yaml -f deploy/compose.laptop.yaml
# Jobs write to bind mounts (data/, eval/, deploy/var, deploy/backups) as you.
export LEXAI_UID := $(shell id -u 2>/dev/null || echo 10001)
export LEXAI_GID := $(shell id -g 2>/dev/null || echo 10001)
ARGS ?=

deploy/.env:
	@echo "deploy/.env is missing: cp deploy/.env.example deploy/.env and set the secrets"; exit 1

images:
	$(COMPOSE) build api web

up: deploy/.env
	$(COMPOSE) up -d --build

up-observability: deploy/.env
	$(COMPOSE) --profile observability up -d --build

up-gpu: deploy/.env
	$(COMPOSE) -f deploy/compose.gpu.yaml up -d --build

up-laptop:
	$(LAPTOP) up -d --build api web

down:
	$(COMPOSE) --profile observability --profile job down

ps:
	$(COMPOSE) --profile observability ps -a

logs:
	$(COMPOSE) --profile observability logs --tail 200 -f $(ARGS)

docker-ingest:  # make docker-ingest ARGS="--only constitution"
	$(COMPOSE) run --rm ingest fetch $(ARGS) && $(COMPOSE) run --rm ingest build $(ARGS)

docker-status:
	$(COMPOSE) run --rm ingest status

docker-eval:  # make docker-eval ARGS="retrieval --configs hybrid"
	$(COMPOSE) run --rm -e LEXAI_COMMIT=$$(git rev-parse --short HEAD) eval $(ARGS)

backup:
	$(COMPOSE) run --rm ingest backup --keep 7

restore:  # make restore ARGS=lexai-20261121T020000Z   (default: latest)
	$(COMPOSE) run --rm ingest restore $(ARGS)

deploy:  # make deploy TAG=1.2.0
	python3 deploy/deploy.py --tag $(TAG)

rollback:
	python3 deploy/deploy.py --rollback

smoke-release:
	$(COMPOSE) run --rm --no-deps smoke --base-url http://web:8080 $(ARGS)
