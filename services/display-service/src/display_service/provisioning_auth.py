"""Persistent shared secret for gateway-only device provisioning calls."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


class ProvisioningTokenStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._path = directory / "gateway-token"

    def load_or_create(self) -> str:
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._directory, 0o700)
        if not self._path.exists():
            token = secrets.token_urlsafe(32)
            try:
                descriptor = os.open(
                    self._path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "w", encoding="ascii") as file:
                    file.write(token)
                    file.flush()
                    os.fsync(file.fileno())
        token = self._path.read_text(encoding="ascii").strip()
        if len(token) < 40 or any(character.isspace() for character in token):
            raise RuntimeError("Provisioning token file is invalid")
        os.chmod(self._path, 0o600)
        return token
