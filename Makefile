.PHONY: install lint format test test-integration build clean

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src

format:
	uv run ruff check --fix .
	uv run ruff format .

test:
	uv run pytest -q -m "not integration"

# Boots a throwaway platform from INSIGHTMON_PLATFORM_DIR (default ../insightmon-ai-eval), creates
# an API key, runs tests/integration and shuts the platform down. With INSIGHTMON_IT_BASE_URL set
# it uses that platform instead (see scripts/integration.sh).
test-integration:
	scripts/integration.sh

build:
	uv build

clean:
	rm -rf dist build .pytest_cache .ruff_cache .mypy_cache
