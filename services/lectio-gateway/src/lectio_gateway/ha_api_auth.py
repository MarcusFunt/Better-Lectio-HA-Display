"""Scoped Home Assistant API token storage using a one-way digest."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class GatewayApiTokenStore:
    """Store only the SHA-256 digest of the scoped API credential."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.digest_path = self.directory / "token.sha256"

    @property
    def managed_digest_exists(self) -> bool:
        try:
            self.digest_path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            # An unreadable managed path must not reactivate a legacy secret.
            return True
        return True

    def create_or_rotate(self) -> str:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest().encode("ascii") + b"\n"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.directory.chmod(0o700)
        fd, temporary_name = tempfile.mkstemp(prefix=".token.", dir=self.directory)
        temporary_path = Path(temporary_name)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(digest)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, self.digest_path)
            if os.name == "posix":
                self.digest_path.chmod(0o600)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        return token

    def matches(self, candidate: str) -> bool:
        try:
            if not stat.S_ISREG(self.digest_path.lstat().st_mode):
                return False
            stored_digest = self.digest_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return False
        if not _SHA256_HEX.fullmatch(stored_digest):
            return False
        supplied_digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        return hmac.compare_digest(stored_digest, supplied_digest)
