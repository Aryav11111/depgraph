# Recipes are single commands with no shell-specific syntax so they behave the
# same under cmd.exe (Windows make) and sh (macOS/Linux/CI).
.DEFAULT_GOAL := help
.PHONY: help install up down logs migrate revision run test lint fmt check

help:  ## List targets
	@uv run python -c "import re;[print(f'{m[0]:<10} {m[1]}') for m in re.findall(r'^([a-z]+):.*?## (.*)$$', open('Makefile').read(), re.M)]"

install:  ## Install locked dependencies (incl. dev) into .venv
	uv sync --frozen

up:  ## Build and start postgres, redis, migrations and the API
	docker compose up -d --build --wait

down:  ## Stop the stack (data volume is kept)
	docker compose down

logs:  ## Tail API logs
	docker compose logs -f api

migrate:  ## Apply migrations from the host
	uv run alembic upgrade head

revision:  ## Autogenerate a migration: make revision m="message"
	uv run alembic revision --autogenerate -m "$(m)"

run:  ## Run the API on the host with autoreload
	uv run uvicorn --factory depgraph.main:create_app --reload --port 8000

test:  ## Run the test suite (needs `make up` for postgres/redis)
	uv run pytest

lint:  ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt:  ## Auto-fix lint issues and format
	uv run ruff check --fix .
	uv run ruff format .

check: lint test  ## Everything CI runs
