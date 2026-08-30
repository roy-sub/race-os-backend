# RaceOS — local development.
#
# Local development is a virtualenv and uvicorn. There is no Docker in this
# project: not for production, not for local development, not for tests.

VENV        ?= .venv
PY          := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
BACKEND     := backend
HOST        ?= 127.0.0.1
PORT        ?= 8000

# The test database is separate from the development one so `make test` can
# drop and rebuild it without touching seeded development data.
TEST_DATABASE_URL ?= postgresql+psycopg://raceos@localhost:5432/raceos_test

.PHONY: help venv install dev test test-unit test-integration test-golden \
        migrate migration downgrade seed check-env check-db lint typecheck fmt clean

help:
	@echo "RaceOS backend"
	@echo ""
	@echo "  make install       create the virtualenv and install dependencies"
	@echo "  make dev           run the API with reload at http://$(HOST):$(PORT)"
	@echo "  make test          the whole suite"
	@echo "  make test-unit     unit tests only (no database needed)"
	@echo "  make test-golden   solver golden-file suite (a diff blocks deploy)"
	@echo "  make migrate       apply migrations to DATABASE_URL"
	@echo "  make migration m=  autogenerate a revision with message m"
	@echo "  make seed          idempotent seed data"
	@echo "  make check-env     validate .env against the required set"
	@echo "  make check-db      verify the database connection form and PostGIS"
	@echo "  make lint          ruff"
	@echo "  make typecheck     mypy (strict on solver/ and domain/)"

$(VENV)/bin/python:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip setuptools wheel

venv: $(VENV)/bin/python

install: venv
	$(PIP) install -r $(BACKEND)/requirements-dev.txt

dev:
	cd $(BACKEND) && ../$(PY) -m uvicorn raceos.api.main:app --reload --host $(HOST) --port $(PORT)

# Production start command, documented here so it is testable locally and
# cannot drift from what Render runs. See render.yaml and the README.
start:
	cd $(BACKEND) && ../$(VENV)/bin/gunicorn raceos.api.main:app \
		--worker-class uvicorn.workers.UvicornWorker \
		--workers $${WEB_CONCURRENCY:-2} \
		--bind 0.0.0.0:$${PORT:-8000} \
		--timeout 120 \
		--access-logfile -

test:
	cd $(BACKEND) && TEST_DATABASE_URL=$(TEST_DATABASE_URL) ../$(PY) -m pytest -q

test-unit:
	cd $(BACKEND) && ../$(PY) -m pytest tests/unit -q

test-integration:
	cd $(BACKEND) && TEST_DATABASE_URL=$(TEST_DATABASE_URL) ../$(PY) -m pytest tests/integration -q

test-golden:
	cd $(BACKEND) && ../$(PY) -m pytest -q -m golden

migrate:
	cd $(BACKEND) && ../$(PY) -m alembic upgrade head

migration:
	@test -n "$(m)" || (echo "usage: make migration m='what changed'" && exit 1)
	cd $(BACKEND) && ../$(PY) -m alembic revision --autogenerate -m "$(m)"

downgrade:
	cd $(BACKEND) && ../$(PY) -m alembic downgrade -1

seed:
	cd $(BACKEND) && ../$(PY) -m raceos.db.seed

check-env:
	$(PY) scripts/check_env.py

check-db:
	$(PY) scripts/check_supabase.py

lint:
	cd $(BACKEND) && ../$(VENV)/bin/ruff check raceos tests

fmt:
	cd $(BACKEND) && ../$(VENV)/bin/ruff check --fix raceos tests && ../$(VENV)/bin/ruff format raceos tests

typecheck:
	cd $(BACKEND) && ../$(VENV)/bin/mypy

clean:
	find . -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache
