PYTHON ?= python3
COMPOSE ?= docker compose

.PHONY: install-dev test lint compose-config compose-build compose-up compose-down

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt
	$(PYTHON) -m pip install -e "services/lectio-gateway[test,lint]" -e "services/lectio-auth-browser" -e "services/lectio-auth-lifecycle[test]" -e "services/display-service[test,lint]"

test:
	$(PYTHON) -m pytest -q tests services/lectio-gateway/tests services/lectio-auth-browser/tests services/lectio-auth-lifecycle/tests services/display-service/tests

lint:
	$(PYTHON) -m ruff check --select E4,E7,E9,F,I services/lectio-gateway services/lectio-auth-browser services/lectio-auth-lifecycle services/display-service tests/test_compose_scaffold.py tests/test_environment_template.py

compose-config:
	$(COMPOSE) config --quiet

compose-build:
	$(COMPOSE) --profile auth-browser build

compose-up:
	$(COMPOSE) --profile auth-browser build lectio-auth-browser
	$(COMPOSE) up --build --detach

compose-down:
	$(COMPOSE) down
