PYTHON ?= python3.12

.PHONY: setup setup-backend setup-frontend dev dev-backend dev-frontend test lint docker-up docker-down

setup: setup-backend setup-frontend

setup-backend:
	cd backend && $(PYTHON) -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

setup-frontend:
	cd frontend && npm ci

dev:
	docker compose up --build

dev-backend:
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

dev-frontend:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/pytest
	cd frontend && npm test

lint:
	cd backend && .venv/bin/ruff check app tests
	cd frontend && npm run lint

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down
