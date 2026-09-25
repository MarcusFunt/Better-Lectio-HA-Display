import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict

from lectio_gateway.auth.browser_client import (
    AuthBrowserClient,
    BrowserUnavailable,
)
from lectio_gateway.lectio.client import LectioClient
from lectio_gateway.lectio.errors import LectioResponseChanged, LectioSessionExpired
from lectio_gateway.lectio.models import AuthenticatedLectioSession

_LOGGER = logging.getLogger(__name__)


class AuthState(StrEnum):
    UNCONFIGURED = "UNCONFIGURED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    STARTING_BROWSER = "STARTING_BROWSER"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    AUTHENTICATED = "AUTHENTICATED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    AUTH_FAILED = "AUTH_FAILED"


class AuthFlowInProgress(RuntimeError):
    """An authentication flow is already active."""


class AuthStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: AuthState
    transitioned_at: AwareDatetime
    school_id: str | None = None
    student_id: str | None = None
    last_verified_at: AwareDatetime | None = None
    error: str | None = None


class AuthManager:
    """Owns browser login, session validation, and durable session storage."""

    def __init__(
        self,
        *,
        data_dir: Path,
        browser_url: str,
        browser_view_url: str,
        login_url: str,
        timeout_seconds: int = 900,
        poll_seconds: float = 2.0,
    ):
        self._data_dir = data_dir
        self._session_path = data_dir / "lectio-session.json"
        self._status_path = data_dir / "auth-status.json"
        self._browser = AuthBrowserClient(browser_url, login_url, timeout_seconds)
        self.browser_view_url = browser_view_url
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.session: AuthenticatedLectioSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._status = AuthStatus(
            state=AuthState.LOGIN_REQUIRED,
            transitioned_at=datetime.now(timezone.utc),
        )

    async def initialize(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self._data_dir, 0o700)
        except OSError:
            _LOGGER.warning("Could not restrict Lectio data directory permissions")
        if self._session_path.exists():
            try:
                raw = await asyncio.to_thread(self._session_path.read_text, encoding="utf-8")
                self.session = AuthenticatedLectioSession.from_json(raw)
                self._set_state(AuthState.AUTHENTICATED)
                return
            except (OSError, ValueError):
                self._set_state(
                    AuthState.AUTH_FAILED,
                    error="The stored Lectio session could not be read. Please log in again.",
                )
                return
        self._set_state(AuthState.LOGIN_REQUIRED)

    def status(self) -> AuthStatus:
        return self._status

    async def start(self) -> AuthStatus:
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise AuthFlowInProgress("Authentication is already in progress")
            self._set_state(AuthState.STARTING_BROWSER, error=None)
            self._task = asyncio.create_task(self._run_login())
            return self._status

    async def cancel(self) -> AuthStatus:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
        await self._browser.stop()
        target = AuthState.AUTHENTICATED if self.session is not None else AuthState.LOGIN_REQUIRED
        self._set_state(target, error=None)
        return self._status

    async def logout(self) -> AuthStatus:
        await self.cancel()
        self.session = None
        try:
            await asyncio.to_thread(self._session_path.unlink, missing_ok=True)
        except OSError:
            _LOGGER.exception("Could not remove the persisted Lectio session")
            self._set_state(
                AuthState.AUTH_FAILED,
                error="The stored Lectio session could not be removed.",
            )
            return self._status
        self._set_state(AuthState.LOGIN_REQUIRED, error=None)
        return self._status

    async def shutdown(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._browser.stop()

    async def _run_login(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        last_fingerprint: str | None = None
        last_validation_at = 0.0
        try:
            await self._browser.start()
            self._set_state(AuthState.WAITING_FOR_USER, error=None)
            while loop.time() < deadline:
                snapshot = await self._browser.snapshot()
                if snapshot.state in {"expired", "error"}:
                    self._set_state(
                        AuthState.AUTH_FAILED,
                        error="The temporary browser session ended before Lectio login completed.",
                    )
                    return
                if snapshot.session is not None:
                    try:
                        candidate = AuthenticatedLectioSession.model_validate(snapshot.session)
                    except ValueError:
                        self._set_state(
                            AuthState.AUTH_FAILED,
                            error="The browser returned an invalid Lectio session.",
                        )
                        return
                    fingerprint = hashlib.sha256(candidate.to_json().encode("utf-8")).hexdigest()
                    should_validate = (
                        fingerprint != last_fingerprint or loop.time() - last_validation_at >= 15
                    )
                    if should_validate:
                        last_fingerprint = fingerprint
                        last_validation_at = loop.time()
                        try:
                            valid = await LectioClient(candidate).validate_session()
                        except LectioSessionExpired:
                            valid = False
                        except LectioResponseChanged:
                            self._set_state(
                                AuthState.WAITING_FOR_USER,
                                error="Lectio returned an unexpected page; finish login and try again.",
                            )
                            valid = False
                        except Exception:
                            self._set_state(
                                AuthState.WAITING_FOR_USER,
                                error="Lectio could not be reached for session validation; retrying.",
                            )
                            valid = False
                        if valid:
                            verified = candidate.model_copy(
                                update={"last_verified_at": datetime.now(timezone.utc)}
                            )
                            await asyncio.to_thread(self._save_session, verified)
                            self.session = verified
                            self._set_state(AuthState.AUTHENTICATED, error=None)
                            await self._browser.complete()
                            return
                await asyncio.sleep(self.poll_seconds)
            self._set_state(
                AuthState.AUTH_FAILED,
                error="Lectio login did not complete before the temporary browser timed out.",
            )
        except asyncio.CancelledError:
            raise
        except BrowserUnavailable:
            self._set_state(
                AuthState.AUTH_FAILED,
                error="The temporary Lectio browser could not be started or reached.",
            )
        except Exception:
            _LOGGER.exception("Lectio browser authentication failed")
            self._set_state(
                AuthState.AUTH_FAILED,
                error="Lectio authentication failed. Review the service logs and try again.",
            )
        finally:
            await self._browser.stop()
            if self._task is asyncio.current_task():
                self._task = None

    def _set_state(self, state: AuthState, *, error: str | None = None) -> None:
        self._status = AuthStatus(
            state=state,
            transitioned_at=datetime.now(timezone.utc),
            school_id=self.session.school_id if self.session else None,
            student_id=self.session.student_id if self.session else None,
            last_verified_at=self.session.last_verified_at if self.session else None,
            error=error,
        )
        try:
            self._write_private_file(self._status_path, self._status.model_dump_json())
        except OSError:
            _LOGGER.warning("Could not persist Lectio authentication status")

    def _save_session(self, session: AuthenticatedLectioSession) -> None:
        self._write_private_file(self._session_path, session.to_json())

    def _write_private_file(self, path: Path, content: str) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._data_dir, 0o700)
        temporary = self._data_dir / f".{path.name}.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
