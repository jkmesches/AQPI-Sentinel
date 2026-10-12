SHELL := /bin/bash

# Same resolution order as scripts/_common.sh: a venv inside the project,
# else one alongside it. Kept in step with that file — two different answers
# to "which python" is how a test passes here and fails under scripts/.
VENV_PY := $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; \
                   else echo ../.venv/bin/python; fi)

.PHONY: dev stop status restart restart-fe restart-be logs pg-up pg-stop pg-shell test-js test-db help

help:
	@echo "make dev          - start postgres + backend + frontend"
	@echo "make stop         - stop backend + frontend (postgres stays up)"
	@echo "make restart      - restart backend + frontend"
	@echo "make restart-fe   - restart only vite (clears its caches first)"
	@echo "make restart-be   - restart only backend"
	@echo "make status       - show what's running"
	@echo "make logs         - tail backend + frontend logs"
	@echo "make pg-up        - start the postgres container"
	@echo "make pg-stop      - stop the postgres container"
	@echo "make pg-shell     - psql into the dev database"
	@echo "make test-js      - run the frontend logic tests (validation_tests/js)"
	@echo "make test-db      - run the validation tests that need postgres"

dev:
	@bash scripts/dev.sh

stop:
	@bash scripts/stop.sh

status:
	@bash scripts/status.sh

restart: stop dev

restart-fe:
	@bash scripts/restart-fe.sh

restart-be:
	@bash scripts/restart-be.sh

logs:
	@bash scripts/logs.sh

pg-up:
	@docker compose -f ops/docker-compose.dev.yml up -d

pg-stop:
	@docker compose -f ops/docker-compose.dev.yml stop

pg-shell:
	@PGPASSWORD=sentinel-dev psql -h 127.0.0.1 -U sentinel -d sentinel

# Frontend logic tests. Each run_*.mjs compiles the real $lib module with
# esbuild and runs its assertions, so they exercise shipped code rather than a
# re-implementation. Discovered by glob — a new run_*.mjs is picked up with no
# edit here.
test-js:
	@rc=0; for f in validation_tests/js/run_*.mjs; do \
	  echo "== $$f"; node "$$f" || rc=1; \
	done; exit $$rc

# The validation tests that need a database. Everything else in
# validation_tests/ runs with no DB and no network, on purpose; these few
# exercise SQL, so a Python stand-in would test a re-implementation rather
# than the query that ships. Each builds and drops its own throwaway schema,
# so this cannot disturb dev data.
#
# Discovered by the marker rather than by a list: a test that reads
# SENTINEL_TEST_DB_URL needs a database by definition, and a hand-kept list
# here is one more enumeration site to forget.
test-db: pg-up
	@rc=0; found=0; \
	for f in $$(grep -ls SENTINEL_TEST_DB_URL validation_tests/test_*.py); do \
	  found=1; echo "── $$f"; \
	  "$(VENV_PY)" "$$f" || rc=1; \
	done; \
	[ "$$found" = 1 ] || echo "no DB-backed tests found"; \
	exit $$rc
