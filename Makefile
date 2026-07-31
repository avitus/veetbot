PYTHON ?= python

.PHONY: install format lint typecheck test check db-up migrate \
	test-static test-contract test-fast test-integration test-live \
	docs docs-serve docs-check citations-fix

install:
	uv sync --all-groups

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy src tests

test:
	uv run pytest -m "not live"

test-static:
	uv run pytest -m static

test-contract:
	@uv run pytest -m "not static and not integration and not live"; \
	status=$$?; test $$status -eq 0 -o $$status -eq 5

test-fast: test-static test-contract

test-integration:
	@uv run pytest -m integration; status=$$?; test $$status -eq 0 -o $$status -eq 5

test-live:
	@RUN_LIVE_MODEL_TESTS=1 uv run pytest -m live; \
	status=$$?; test $$status -eq 0 -o $$status -eq 5

docs:
	uv run $(PYTHON) scripts/build_docs.py

docs-serve:
	uv run mkdocs serve

docs-check:
	uv run $(PYTHON) scripts/check_docs.py

citations-fix:
	uv run $(PYTHON) scripts/check_citations.py --update

check: lint typecheck test-fast docs-check

db-up:
	docker compose up -d postgres
	@attempt=0; \
	pg_container_id=$$(docker compose ps -q postgres); \
	test -n "$$pg_container_id"; \
	while test "$$(docker inspect --format '{{.State.Health.Status}}' "$$pg_container_id")" != healthy; do \
		attempt=$$((attempt + 1)); \
		if test $$attempt -ge 30; then \
			docker compose ps postgres; \
			exit 1; \
		fi; \
		sleep 2; \
	done

migrate:
	uv run alembic upgrade head
