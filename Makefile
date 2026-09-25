PYTHON ?= python3
COMPOSE ?= docker compose

.PHONY: install-dev test lint compose-config compose-build compose-up compose-down

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt
	$(PYTHON) -m pip install -e "services/lectio-gateway[test,lint]" -e "services/display-service[test,lint]"

test:
	$(PYTHON) -m pytest -q tests services/lectio-gateway/tests services/display-service/tests

lint:
	$(PYTHON) -m ruff check services/lectio-gateway services/lectio-auth-browser services/display-service tests/test_compose_scaffold.py tests/test_environment_template.py

compose-config:
	$(COMPOSE) config --quiet

compose-build:
	$(COMPOSE) build

compose-up:
	$(COMPOSE) up --build --detach

compose-down:
	$(COMPOSE) down
