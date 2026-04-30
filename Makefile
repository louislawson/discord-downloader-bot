.PHONY: install install-dev test test-cov lint format format-check check precommit precommit-install clean dev down logs

PYTHON ?= python
PIP    ?= $(PYTHON) -m pip

install:
	$(PIP) install '.[azure]'

install-dev:
	$(PIP) install -r requirements-dev.txt
	pre-commit install

test:
	$(PYTHON) -m pytest

test-cov:
	$(PYTHON) -m pytest --cov=downloader_bot --cov-report=term-missing --cov-report=html

lint:
	$(PYTHON) -m ruff check downloader_bot tests

format:
	$(PYTHON) -m ruff format downloader_bot tests
	$(PYTHON) -m ruff check --fix downloader_bot tests

format-check:
	$(PYTHON) -m ruff format --check downloader_bot tests

check: lint format-check test

precommit:
	pre-commit run --all-files

precommit-install:
	pre-commit install

dev:
	docker compose up

down:
	docker compose down

logs:
	docker compose logs -f bot worker

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} +
