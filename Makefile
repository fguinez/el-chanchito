.PHONY: help install install-deps install-py dev dev-web build db-up db-down db-migrate db-reset db-shell \
       scrapers-once scrapers-start scrapers-test fintual-login bci-lider-login up down logs clean typecheck lint \
       test test-ts test-py test-db test-db-up test-db-down product-model-generate

# ─── Help ────────────────────────────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Setup ───────────────────────────────────────────────────────────────────

install: install-deps ## Install all dependencies (Node + Python + Playwright browser)
	.venv/bin/playwright install chromium

install-deps: install-py ## Install Node + Python dependencies, without the Playwright browser
	pnpm install

install-py: ## Create .venv and install the scraper runtime + test dependencies
	python3 -m venv .venv
	# pip resolves the `-e ../../packages/product-model` line relative to its
	# working directory, so install from apps/scrapers (the Dockerfile does the
	# same via WORKDIR).
	cd apps/scrapers && ../../.venv/bin/pip install -r requirements-dev.txt

env: ## Create .env from .env.example
	@test -f .env || (cp .env.example .env && echo "Created .env — edit it with your credentials")
	@test -f .env && echo ".env already exists"

# ─── Secrets (macOS Keychain) ────────────────────────────────────────────────
# Secrets live in the login Keychain as generic passwords named "chanchito.<VAR>".
# Non-secret config (RUT, emails, hosts) stays in .env.

SECRET_KEYS := BANCHILE_PASSWORD LIDER_BCI_PASSWORD FINTUAL_PASSWORD BUDA_API_KEY BUDA_API_SECRET EMAIL_IMAP_PASSWORD DASHBOARD_PASSWORD

secrets-init: ## Store scraper secrets in macOS Keychain (interactive, skips existing)
	@for k in $(SECRET_KEYS); do \
		if security find-generic-password -a "$$USER" -s "chanchito.$$k" >/dev/null 2>&1; then \
			echo "= $$k already stored (overwrite with: make secret-set KEY=$$k)"; \
		else \
			echo "-> Enter value for $$k"; \
			security add-generic-password -a "$$USER" -s "chanchito.$$k" -w && echo "+ $$k stored"; \
		fi; \
	done

secret-set: ## Store or overwrite one secret (usage: make secret-set KEY=FINTUAL_PASSWORD)
	@test -n "$(KEY)" || { echo "Usage: make secret-set KEY=<VAR_NAME>"; exit 1; }
	@security add-generic-password -U -a "$$USER" -s "chanchito.$(KEY)" -w && echo "+ chanchito.$(KEY) stored"

secrets-status: ## Show which secrets are present in Keychain
	@for k in $(SECRET_KEYS); do \
		security find-generic-password -a "$$USER" -s "chanchito.$$k" >/dev/null 2>&1 \
			&& echo "+ $$k" || echo "x $$k (missing)"; \
	done

# ─── Database ────────────────────────────────────────────────────────────────

db-up: ## Start PostgreSQL container
	docker compose up -d postgres
	@echo "PostgreSQL running on port $${POSTGRES_PORT:-5435}"

db-down: ## Stop PostgreSQL container
	docker compose stop postgres

db-migrate: ## Run database migrations
	DATABASE_URL=$${DATABASE_URL:-postgres://finance:finance@localhost:5435/finance} \
		pnpm --filter @chanchito/db-schema migrate

db-reset: ## Drop and recreate database (DESTRUCTIVE)
	@echo "This will delete all data. Press Ctrl+C to cancel, Enter to continue..."
	@read _confirm
	docker compose exec postgres dropdb -U $${POSTGRES_USER:-finance} $${POSTGRES_DB:-finance} --if-exists
	docker compose exec postgres createdb -U $${POSTGRES_USER:-finance} $${POSTGRES_DB:-finance}
	$(MAKE) db-migrate

db-shell: ## Open psql shell
	docker compose exec postgres psql -U $${POSTGRES_USER:-finance} $${POSTGRES_DB:-finance}

db-dump: ## Dump database to file
	docker compose exec postgres pg_dump -U $${POSTGRES_USER:-finance} $${POSTGRES_DB:-finance} > dump_$$(date +%Y%m%d_%H%M%S).sql
	@echo "Dump saved"

# ─── Dashboard ───────────────────────────────────────────────────────────────

dev: ## Start the full local stack: postgres + scrapers (with on-demand refresh) + dashboard
	./scripts/dev.sh

dev-web: ## Start only the Next.js dev server (assumes postgres is already up)
	DATABASE_URL=$${DATABASE_URL:-postgres://finance:finance@localhost:5435/finance} \
		pnpm --filter @chanchito/web dev

build: ## Build the dashboard for production
	pnpm --filter @chanchito/web build

typecheck: ## Run TypeScript type checking
	pnpm --filter @chanchito/web exec tsc --noEmit

lint: ## Run ESLint
	pnpm --filter @chanchito/web lint

test: test-ts test-py ## Run all tests (TypeScript + Python)

test-ts: ## Run TypeScript tests only
	pnpm --filter @chanchito/web test

test-py: ## Run Python tests only (DB tests skip unless TEST_DATABASE_URL is set)
	cd apps/scrapers && ../../.venv/bin/python -m pytest tests/ -v
	cd packages/product-model && ../../.venv/bin/python -m pytest tests/ -v

# The DB tests create and drop their own chanchito_test_* databases on the
# server TEST_DATABASE_URL names, so it must be a disposable one: this target
# points it at the tmpfs `postgres-test` service, never at the dev database.
test-db: test-db-up ## Run the Python tests, DB tests included, against the throwaway test PostgreSQL
	TEST_DATABASE_URL=postgres://finance:finance@127.0.0.1:$${POSTGRES_TEST_PORT:-5436}/postgres \
		$(MAKE) test-py

test-db-up: ## Start the throwaway test PostgreSQL (tmpfs, port 5436)
	docker compose --profile test up -d --wait postgres-test

test-db-down: ## Stop and remove the test PostgreSQL container (its data goes with it)
	docker compose --profile test rm -sf postgres-test

# ─── Product model ───────────────────────────────────────────────────────────

product-model-generate: ## Regenerate product-model artifacts (TS + JSON Schema + PRODUCTS.md)
	.venv/bin/python packages/product-model/scripts/generate.py

# ─── Scrapers ────────────────────────────────────────────────────────────────

scrapers-once: ## Run all scrapers once and exit
	@. ./scripts/load-secrets.sh && cd apps/scrapers && \
		DATABASE_URL=$${DATABASE_URL:-postgres://finance:finance@localhost:5435/finance} \
		SCRAPER_MODE=once \
		../../.venv/bin/python main.py

scrapers-start: ## Start scrapers on schedule (long-running, control endpoint on :8080)
	@. ./scripts/load-secrets.sh && cd apps/scrapers && \
		DATABASE_URL=$${DATABASE_URL:-postgres://finance:finance@localhost:5435/finance} \
		SCRAPER_MODE=scheduled \
		SCRAPER_CONTROL_PORT=$${SCRAPER_CONTROL_PORT:-8080} \
		../../.venv/bin/python main.py

fintual-login: ## Sign in to Fintual (prompts for the e-mailed 2FA code) and cache the session
	@. ./scripts/load-secrets.sh && cd apps/scrapers && \
		../../.venv/bin/python -m scrapers.institutions.fintual

bci-lider-login: ## Tarjeta Lider Bci reuse mode (optional; managed is default): launch a real Chrome, autofill the login, leave it running; then set LIDER_BCI_CDP_URL
	@. ./scripts/load-secrets.sh && cd apps/scrapers && \
		../../.venv/bin/python -m scrapers.institutions.bci_lider

scrapers-test: ## Test scraper imports and basic functionality
	cd apps/scrapers && ../../.venv/bin/python -c "from scrapers.institutions import FintualScraper, BudaScraper, BanChileScraper, BciLiderScraper, MachScraper, MercadoPagoScraper, TenpoScraper; from scrapers.backends.email import get_session, fetch_transactions_for_pattern; from scrapers.backends.banchile_movements import fetch_session; from db.writer import start_scraper_run, finish_scraper_run; print('All scraper imports OK')"

# ─── Docker (full stack) ────────────────────────────────────────────────────

up: ## Start all services (postgres + web + scrapers)
	@. ./scripts/load-secrets.sh && docker compose up -d

down: ## Stop all services
	docker compose down

logs: ## Tail logs from all services
	docker compose logs -f

logs-web: ## Tail dashboard logs
	docker compose logs -f web

logs-scrapers: ## Tail scraper logs
	docker compose logs -f scrapers

# ─── Utilities ───────────────────────────────────────────────────────────────

seed-legacy-history: ## Seed synthetic pre-migration wealth snapshots for demos
	@echo "Seeding synthetic legacy wealth snapshots..."
	@created=0; \
	for row in \
		'{"snapshotDate":"2023-02-01","patrimonio":1000000,"deuda":0}' \
		'{"snapshotDate":"2023-04-01","patrimonio":1500000,"deuda":200000}' \
		'{"snapshotDate":"2023-05-01","patrimonio":2000000,"deuda":200000}' \
		'{"snapshotDate":"2023-06-01","patrimonio":2500000,"deuda":300000}' \
		'{"snapshotDate":"2023-09-01","patrimonio":3500000,"deuda":300000}' \
		'{"snapshotDate":"2023-10-01","patrimonio":4000000,"deuda":250000}' \
		'{"snapshotDate":"2023-11-01","patrimonio":4500000,"deuda":250000}' \
		'{"snapshotDate":"2024-01-01","patrimonio":5500000,"deuda":400000}' \
		'{"snapshotDate":"2024-06-01","patrimonio":8000000,"deuda":500000}' \
		'{"snapshotDate":"2024-07-01","patrimonio":8500000,"deuda":500000}' \
		'{"snapshotDate":"2024-10-01","patrimonio":11000000,"deuda":600000}' \
		'{"snapshotDate":"2024-11-01","patrimonio":12000000,"deuda":600000}' \
		'{"snapshotDate":"2025-12-01","patrimonio":18000000,"deuda":700000,"fintualBalance":10000000,"mercadopagoBalance":2000000,"banchileSavings":3000000}' \
		'{"snapshotDate":"2026-01-01","patrimonio":19000000,"deuda":700000,"fintualBalance":10500000,"mercadopagoBalance":2200000,"banchileSavings":3100000}' \
		'{"snapshotDate":"2026-03-01","patrimonio":20000000,"deuda":800000,"fintualBalance":11000000,"mercadopagoBalance":2400000,"banchileSavings":3200000}'; do \
		status=$$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:3000/api/wealth \
			-H "Content-Type: application/json" -d "$$row"); \
		if [ "$$status" = 201 ]; then created=$$((created + 1)); \
		else echo "  HTTP $$status for $$row"; fi; \
	done; \
	echo "Done: $$created of 15 snapshots created"

seed-category-rules: ## Seed default category assignment rules
	@echo "Adding category rules..."
	@CATS=$$(curl -s http://localhost:3000/api/categories) && \
	TRANSPORT=$$(echo $$CATS | python3 -c "import json,sys;print(next(c['id'] for c in json.load(sys.stdin) if c['name']=='Transporte'))") && \
	FOOD=$$(echo $$CATS | python3 -c "import json,sys;print(next(c['id'] for c in json.load(sys.stdin) if c['name']=='Restaurantes'))") && \
	SUPER=$$(echo $$CATS | python3 -c "import json,sys;print(next(c['id'] for c in json.load(sys.stdin) if c['name']=='Supermercado'))") && \
	curl -s -X POST http://localhost:3000/api/categories -H "Content-Type: application/json" -d "{\"keyword\":\"uber\",\"categoryId\":\"$$TRANSPORT\",\"priority\":10}" > /dev/null && \
	curl -s -X POST http://localhost:3000/api/categories -H "Content-Type: application/json" -d "{\"keyword\":\"rappi\",\"categoryId\":\"$$FOOD\",\"priority\":10}" > /dev/null && \
	curl -s -X POST http://localhost:3000/api/categories -H "Content-Type: application/json" -d "{\"keyword\":\"supermercado\",\"categoryId\":\"$$SUPER\",\"priority\":10}" > /dev/null && \
	curl -s -X POST http://localhost:3000/api/categories -H "Content-Type: application/json" -d "{\"keyword\":\"lider\",\"categoryId\":\"$$SUPER\",\"priority\":10}" > /dev/null && \
	echo "Done: 4 rules seeded"

seed-all: seed-legacy-history seed-category-rules ## Seed all default data

categorize: ## Run category auto-assignment on all uncategorized transactions
	@curl -s -X PUT http://localhost:3000/api/categories | python3 -m json.tool

clean: ## Remove build artifacts and volumes
	rm -rf apps/web/.next apps/web/node_modules/.cache
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
