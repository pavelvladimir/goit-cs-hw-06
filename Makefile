.DEFAULT_GOAL := help
# SDK generation and Next export share output directories.
.NOTPARALLEL:
export BACKUP_DIR ?= backups/manual
export RESTORE_DATABASE ?= restore_manual
export WRITE_LOAD_SECONDS ?= 10
export BASELINE ?=
export UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export NEXT_TELEMETRY_DISABLED := 1
.PHONY: help init sync sync-frontend build-frontend dev-frontend lint-frontend test-frontend audit-frontend up down logs check lint typecheck test test-integration coverage audit export-requirements

help: ## Показати команди
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

init: ## Створити відсутні .env і випадкові паролі
	uv run --locked python -m scripts.init_local

sync: sync-python sync-frontend ## Встановити Python і frontend залежності

sync-python: ## Встановити залежності Python 3.13
	uv sync --locked

sync-frontend: ## Встановити залежності Next.js із lockfile (Node.js 22, pnpm)
	pnpm --dir frontend install --frozen-lockfile

build-frontend: sync-frontend check-api ## Зібрати статичний Next.js export, перевірити TypeScript і HTML
	pnpm --dir frontend build

dev-frontend: sync-frontend check-api ## Next.js dev на :3001; запити до Python на :3000
	pnpm --dir frontend dev

lint-frontend: generate-client ## Перевірити Next.js, React, SonarJS і Prettier
	pnpm --dir frontend lint

test-frontend: build-frontend install-browsers ## Chromium, Firefox, WebKit, axe і порівняння вигляду
	pnpm --dir frontend test

audit-frontend: sync-frontend ## Аудит усіх frontend залежностей
	pnpm --dir frontend audit

up: init ## Зібрати та запустити застосунок і MongoDB
	docker compose up --build -d --wait

down: ## Зупинити контейнери зі збереженням даних
	docker compose down

logs: ## Показати журнали обох сервісів
	docker compose logs --tail=100

lint: lint-python lint-frontend lint-docs ## Перевірити Python, frontend і Markdown

lint-python: ## Перевірити Ruff і форматування Python
	uv run --locked ruff check .
	uv run --locked ruff format --check .

typecheck: ## Перевірити strict mypy
	uv run --locked mypy

test: build-frontend ## Виконати локальні тести без Docker
	uv run --locked pytest -m 'not integration'

coverage: build-frontend ## Виміряти покриття локальними тестами
	uv run --locked coverage run -m pytest -m 'not integration'
	$(MAKE) coverage-report

coverage-report: ## Текстовий, HTML та XML звіти coverage
	uv run --locked coverage report
	uv run --locked coverage html
	uv run --locked coverage xml

format: sync-frontend generate-client ## Форматувати Python і frontend
	uv run --locked ruff format .
	uv run --locked ruff check --fix .
	uv run --locked ruff format .
	pnpm --dir frontend format

check-unit: lint typecheck test-client coverage ## Локальні перевірки без Docker і браузерів

check: check-unit test-frontend test-integration ## Усі перевірки включно з браузерами й Docker

test-integration: build-frontend install-browsers ## Перевірити HTTP → TCP → MongoDB і volume в окремому Compose-проєкті
	uv run --locked pytest --integration -m integration -v

export-requirements: ## Експортувати runtime requirements з uv.lock (із hashes)
	uv export --locked --no-dev --no-emit-project --format requirements-txt -o requirements.txt

audit: ## Перевірити всі Python-залежності (потрібна мережа)
	@mkdir -p artifacts
	uv export --locked --all-groups --no-emit-project --format requirements-txt -o artifacts/audit-requirements.txt --quiet
	uv run --locked pip-audit --cache-dir .cache/pip-audit --require-hashes --disable-pip --progress-spinner off -r artifacts/audit-requirements.txt

.PHONY: audit-image audit-python-sbom audit-frontend-sbom audit-all

audit-image: ## Зібрати кінцевий образ, перевірити CVE та зберегти SBOM і звіт
	uv run --locked python -m scripts.audit_image

audit-python-sbom: ## SBOM і CVE для Python runtime та всіх залежностей uv.lock
	uv run --locked python -m scripts.audit_dependencies --target python

audit-frontend-sbom: ## SBOM, CVE та перевірка опублікованих frontend-файлів
	uv run --locked python -m scripts.audit_dependencies --target frontend

audit-all: ## Усі SBOM, образ, pip-audit і pnpm audit; BASELINE задає попередній звіт
	uv run --locked python -m scripts.audit_dependencies --target all $${BASELINE:+--baseline "$$BASELINE"}

.PHONY: sync-python check-unit lint-docs test-client install-browsers generate-api generate-client check-api lint-python format test-all coverage-report

test-all: test test-client test-frontend test-integration ## Усі локальні, браузерні та інтеграційні тести

lint-docs: sync-frontend ## Перевірити Markdown
	pnpm --dir frontend lint:docs

test-client: generate-client ## Перевірити API клієнт і Zod
	pnpm --dir frontend test:unit

install-browsers: sync-frontend ## Встановити три браузери Playwright
	pnpm --dir frontend exec playwright install chromium firefox webkit

generate-api: sync-frontend ## Оновити OpenAPI snapshot і TypeScript/Zod SDK
	uv run --locked python -m scripts.export_openapi
	pnpm --dir frontend generate

generate-client: sync-frontend check-api ## Згенерувати SDK із перевіреного контракту
	pnpm --dir frontend generate

check-api: ## Виявити розбіжності Python API та OpenAPI snapshot
	uv run --locked python -m scripts.export_openapi --check

.PHONY: backup restore test-resilience check-linux test-write-load

backup: ## Зупинити записи, створити нову backup-папку й відновити app
	@mkdir -p backups
	uv run --locked python -m scripts.database_archive backup "$$BACKUP_DIR"

restore: ## Відновити backup у нову restore_* БД, без перезапису наявної
	uv run --locked python -m scripts.database_archive restore "$$BACKUP_DIR" --database "$$RESTORE_DATABASE"

test-resilience: build-frontend ## Перевірити перевантаження, втрату ACK і відновлення backup у Docker
	uv run --locked pytest --integration -m integration -v -k 'bounded_load or lost_acknowledgement or backup_restores or archive_lock'

test-write-load: build-frontend ## Виміряти справжні POST-записи при паралельності 1/8/16 та звірити MongoDB
	uv run --locked pytest --integration -m integration -v -k real_write_load --write-load-seconds "$$WRITE_LOAD_SECONDS"

check-linux: ## Локальні тести й три браузери у чистому Linux-контейнері
	docker build -f docker/ci.Dockerfile -t goit-cs-hw-06-ci .
	docker run --rm --init --shm-size=1g goit-cs-hw-06-ci
