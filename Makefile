.PHONY: run crawl test lint

run:
	uv run uvicorn app.main:app --reload

crawl:
	uv run python -m app.crawler

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
