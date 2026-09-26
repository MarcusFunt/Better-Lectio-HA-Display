import hashlib
import importlib
import os
from importlib.util import find_spec

import pytest


def _auth_module():
    module_name = "lectio_gateway.ha_api_auth"
    if find_spec(module_name) is None:
        pytest.fail("The gateway API token store is not implemented")
    return importlib.import_module(module_name)


def test_gateway_api_token_store_persists_only_digest_and_rotates(tmp_path):
    store = _auth_module().GatewayApiTokenStore(tmp_path / "api-auth")

    first = store.create_or_rotate()
    first_digest = hashlib.sha256(first.encode("utf-8")).hexdigest()

    assert first
    assert store.digest_path.read_text(encoding="ascii").strip() == first_digest
    assert first not in store.digest_path.read_text(encoding="ascii")
    assert store.matches(first)
    if os.name == "posix":
        assert store.digest_path.stat().st_mode & 0o777 == 0o600

    second = store.create_or_rotate()

    assert second != first
    assert store.matches(second)
    assert not store.matches(first)


def test_corrupt_managed_api_token_digest_fails_closed(tmp_path):
    store = _auth_module().GatewayApiTokenStore(tmp_path / "api-auth")
    store.digest_path.parent.mkdir(parents=True)
    store.digest_path.write_text("not-a-sha256-digest", encoding="ascii")

    assert not store.matches("legacy-environment-token")
    assert store.managed_digest_exists


def test_unreadable_managed_digest_path_still_disables_environment_fallback(tmp_path):
    if os.name != "posix":
        pytest.skip("Symlink behavior is verified on the Linux container runtime")
    store = _auth_module().GatewayApiTokenStore(tmp_path / "api-auth")
    store.digest_path.parent.mkdir(parents=True)
    store.digest_path.symlink_to(store.digest_path.parent / "missing-digest")

    assert store.managed_digest_exists
    assert not store.matches("legacy-environment-token")
