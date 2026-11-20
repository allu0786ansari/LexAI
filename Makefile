.PHONY: requirements install lint format typecheck test check ingest ingest-dry serve smoke

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

ingest:
	cd $(BACKEND) && uv run lexai-ingest

ingest-dry:
	cd $(BACKEND) && uv run lexai-ingest --dry-run

serve:
	cd $(BACKEND) && uv run uvicorn lexai.api.app:create_app --factory --port 8000 --reload

smoke:
	cd $(BACKEND) && uv run python scripts/smoke_phase1.py

# Regenerate the pip requirement files (used on Windows without uv) from uv.lock.
requirements:
	cd $(BACKEND) && H="# Generated from uv.lock - do not edit. Regenerate: make requirements" && \
	  { echo "$$H"; uv export --no-hashes --no-emit-project --no-dev --frozen -q | grep -v '^ *#'; } > requirements.txt && \
	  { echo "$$H"; echo "-r requirements.txt"; uv export --no-hashes --no-emit-project --only-group dev --frozen -q | grep -v '^ *#'; } > requirements-dev.txt
