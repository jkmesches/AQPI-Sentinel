SHELL := /bin/bash

.PHONY: dev stop status restart restart-fe restart-be logs pg-up pg-stop pg-shell help

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
