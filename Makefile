PYTHON ?= python3
COMPOSE ?= docker compose

.PHONY: install-dev install-provision-host test test-home-assistant lint compose-config compose-build compose-up compose-down firmware-build firmware-flash provision-device

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt
	$(PYTHON) -m pip install -e "services/lectio-gateway[test,lint]" -e "services/lectio-auth-browser" -e "services/lectio-auth-lifecycle[test]" -e "services/display-service[test,lint]"

install-provision-host:
	$(PYTHON) -m pip install -r tools/provision/requirements.txt

test:
	$(PYTHON) -m pytest -q tests services/lectio-gateway/tests services/lectio-auth-browser/tests services/lectio-auth-lifecycle/tests services/display-service/tests

test-home-assistant:
	$(PYTHON) -m pytest -q home-assistant/tests

validate-hacs:
	$(PYTHON) -m pytest -q home-assistant/tests/test_manifest.py

lint:
	$(PYTHON) -m ruff check --select E4,E7,E9,F,I services/lectio-gateway services/lectio-auth-browser services/lectio-auth-lifecycle services/display-service custom_components home-assistant tools/provision tests/test_usb_provisioning.py tests/test_compose_scaffold.py tests/test_environment_template.py

compose-config:
	$(COMPOSE) config --quiet

compose-build:
	$(COMPOSE) --profile auth-browser build

compose-up:
	$(COMPOSE) --profile auth-browser build lectio-auth-browser
	$(COMPOSE) up --build --detach

compose-down:
	$(COMPOSE) down

firmware-build:
	pio run -d firmware -e lectio_s3

firmware-flash:
	pio run -d firmware -e lectio_s3 -t upload

provision-device:
	$(PYTHON) tools/provision/provision_device.py
