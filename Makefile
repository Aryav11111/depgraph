# Recipes are single commands with no shell-specific syntax so they behave the
# same under cmd.exe (Windows make) and sh (macOS/Linux/CI).
#
# Python tooling runs in the `dev` container by default: the only host requirement
# is Docker, and it sidesteps host policies (e.g. Windows Smart App Control) that
# block virtualenv launchers. `make test LOCAL=1` uses the host .venv instead.
ifdef LOCAL
RUN := uv run
else
RUN := docker compose run --rm --build dev
endif

.DEFAULT_GOAL := help
.PHONY: help install up down logs migrate revision test lint fmt check

help:  ## List targets
	@$(RUN) python scripts/make_help.py

install:  ## Host-only: create .venv from uv.lock (for editor tooling / LOCAL=1)
	uv sync --frozen

up:  ## Build and start postgres, redis, migrations and the API on :8080
	docker compose up -d --build --wait

down:  ## Stop the stack (data volume is kept)
	docker compose down

logs:  ## Tail API logs
	docker compose logs -f api

migrate:  ## Apply migrations
	$(RUN) alembic upgrade head

revision:  ## Autogenerate a migration: make revision m="message"
	$(RUN) alembic revision --autogenerate -m "$(m)"

test:  ## Run the test suite (needs `make up` for postgres/redis)
	$(RUN) pytest

lint:  ## Lint and check formatting
	$(RUN) ruff check .
	$(RUN) ruff format --check .

fmt:  ## Auto-fix lint issues and format
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

check: lint test  ## Everything CI runs
