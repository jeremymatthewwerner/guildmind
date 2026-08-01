.PHONY: check determinism evaluator-image format lint schemas sync test typecheck

EVALUATOR_IMAGE ?= guildmind/evaluator:stage1-dev

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

evaluator-image:
	docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
		--build-arg SOURCE_DATE_EPOCH=0 --load --tag $(EVALUATOR_IMAGE) containers/evaluator

check: lint typecheck test
