import json
from pathlib import Path

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "better_lectio"


def test_manifest_declares_versioned_configurable_custom_integration():
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())

    assert manifest["domain"] == "better_lectio"
    assert manifest["config_flow"] is True
    assert manifest["version"] == "0.1.0"
    assert manifest["integration_type"] == "service"


def test_custom_component_translation_is_valid_json():
    translations = json.loads(
        (INTEGRATION / "translations" / "en.json").read_text()
    )

    assert translations["config"]["step"]["user"]["data"]["url"]
    assert translations["options"]["step"]["init"]["data"]["refresh_interval"]
