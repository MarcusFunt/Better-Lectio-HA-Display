import json
from pathlib import Path

INTEGRATION = Path(__file__).parents[2] / "custom_components" / "better_lectio"


def test_manifest_declares_versioned_configurable_custom_integration():
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())

    assert manifest["domain"] == "better_lectio"
    assert manifest["config_flow"] is True
    assert manifest["version"] == "0.1.0"
    assert manifest["integration_type"] == "service"
    assert manifest["issue_tracker"].endswith("/issues")


def test_repository_declares_hacs_integration_metadata():
    hacs = json.loads((INTEGRATION.parents[1] / "hacs.json").read_text())

    assert hacs["name"] == "Better Lectio"


def test_custom_component_translation_is_valid_json():
    translations = json.loads(
        (INTEGRATION / "translations" / "en.json").read_text()
    )

    assert translations["config"]["step"]["user"]["data"]["url"]
    assert translations["config"]["step"]["user"]["data"]["api_token"]
    assert translations["options"]["step"]["init"]["data"]["refresh_interval"]
