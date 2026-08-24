SHELL := /bin/bash
IMAGE ?= kubernetes-lite:dev

.PHONY: help install dev lint test up down build backup logs clean

help:
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "%-10s %s\n", $$1, $$2}'

install: ## Install runtime + dev dependencies
	pip install -r requirements.txt -r requirements-dev.txt

lint: ## Run flake8
	flake8 app tests

test: ## Run pytest
	pytest -v

build: ## Build the Docker image
	docker build -t $(IMAGE) .

up: ## Start the stack (builds image)
	docker compose up -d --build

down: ## Stop the stack
	docker compose down

logs: ## Follow container logs
	docker compose logs -f kubernetes-lite

backup: ## Trigger a database snapshot via the API
	curl -X POST http://localhost:5000/api/maintenance/backup

clean: ## Remove local db, caches, backups
	rm -rf .pytest_cache __pycache__ app/__pycache__ backups kubernetes_lite.db
