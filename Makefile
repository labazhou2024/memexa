# Common developer tasks. Run ``make help`` for the menu.

.PHONY: help install fmt lint test smoke pii-scan demo-ingest demo-query clean dashboard backend-up backend-down

help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "Setup"
	@echo "  install         pip install -e .[dev]"
	@echo "  fmt             ruff format + black"
	@echo "  lint            ruff check + black --check"
	@echo ""
	@echo "Test"
	@echo "  test            unit tests (no backend required)"
	@echo "  smoke           boot backend, ingest demo, run all 8 subcommands"
	@echo "  pii-scan        scan whole tree for residual PII tokens"
	@echo ""
	@echo "Backend"
	@echo "  backend-up      docker compose up -d (postgres + hindsight + dashboard)"
	@echo "  backend-down    docker compose down"
	@echo "  dashboard       open the dashboard in your browser"
	@echo ""
	@echo "Demo"
	@echo "  demo-ingest     ingest the bundled demo dataset"
	@echo "  demo-query      run all 8 subcommands against the demo dataset"
	@echo ""
	@echo "Cleanup"
	@echo "  clean           remove .venv, build artefacts, caches"

install:
	pip install -e ".[dev]"
	pre-commit install

fmt:
	ruff check --fix memexa tests
	ruff format memexa tests
	black memexa tests

lint:
	ruff check memexa tests
	black --check memexa tests

test:
	pytest -m "not integration and not e2e" -ra

pii-scan:
	@bash scripts/pre-commit-pii-scan.sh || (echo "(running full-tree scan, not just staged)"; \
	    bash scripts/full_pii_scan.sh && echo "✅ no residual PII" || exit 1)

backend-up:
	docker compose -f docker-compose.example.yml up -d
	@echo "Waiting for hindsight to be healthy..."
	@until curl -sf http://127.0.0.1:8888/healthz >/dev/null 2>&1; do sleep 1; done
	@echo "✅ backend ready: http://127.0.0.1:8888"

backend-down:
	docker compose -f docker-compose.example.yml down

dashboard:
	@python -m webbrowser http://127.0.0.1:8765 || true

demo-ingest:
	python -m examples.demo_dataset.ingest

demo-query:
	@echo "--- quick ---"
	python -m memexa.core.memory_query quick "demo"
	@echo "--- topic ---"
	python -m memexa.core.memory_query topic "demo"
	@echo "--- timeline ---"
	python -m memexa.core.memory_query timeline --start 2024-01-01 --end 2024-02-01
	@echo "--- pending ---"
	python -m memexa.core.memory_query pending

smoke: backend-up demo-ingest demo-query

clean:
	rm -rf .venv .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.egg-info" -type d -exec rm -rf {} + 2>/dev/null || true
