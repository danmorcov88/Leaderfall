# Leaderfall — developer entry points.
# Requires: Docker with Compose v2, Python 3.12+.

PYTHON  ?= python3
VENV    ?= .venv
BIN     := $(VENV)/bin
PIP     := $(BIN)/pip
LF      := $(BIN)/leaderfall

PROFILE ?= default
SYNC    ?= off
TAG     ?= core

.PHONY: help install lint format test up status chaos chaos-all report down clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(VENV)/.stamp: pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	@touch $@

install: $(VENV)/.stamp ## Create the venv and install leaderfall with dev tools

lint: install ## ruff (lint + format check) and mypy
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy

format: install ## Auto-format with ruff
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

test: install ## Unit tests (no cluster needed)
	$(BIN)/pytest -m "not integration"

up: install ## Start the cluster and wait until it is healthy
	$(LF) up --profile $(PROFILE) --sync $(SYNC)

status: install ## Show cluster topology
	$(LF) status

chaos: install ## Run the smoke scenario (primary-sigkill)
	$(LF) run primary-sigkill

chaos-all: install ## Run the whole suite (TAG=core|advanced|all)
	$(LF) run --all --tag $(TAG)

report: install ## Build Markdown + HTML report from the last run
	$(LF) report

down: install ## Stop the cluster (keeps volumes)
	$(LF) down

clean: ## Remove the venv and caches
	rm -rf $(VENV) .mypy_cache .ruff_cache .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
