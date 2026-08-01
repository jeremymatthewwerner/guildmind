.PHONY: check determinism format lint schemas sync test typecheck

sync:
	uv sync

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy

test:
	uv run pytest

schemas:
	uv run guildmind schemas export

determinism:
	uv run python scripts/check_determinism.py --repetitions 100

check: lint typecheck test
