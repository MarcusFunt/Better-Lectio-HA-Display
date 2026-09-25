import asyncio
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from lectio_gateway.auth.manager import AuthManager, AuthState
from lectio_gateway.data_service import (
    LectioAuthenticationRequired,
    LectioDataService,
    LectioSourceUnavailable,
    LectioStudentIdRequired,
)
from lectio_gateway.lectio.errors import LectioSessionExpired
from lectio_gateway.lectio.models import (
    AuthenticatedLectioSession,
    LectioCookie,
    LectioLesson,
)

START = datetime(2026, 9, 25, tzinfo=timezone.utc)
END = datetime(2026, 9, 26, tzinfo=timezone.utc)


def make_session():
    return AuthenticatedLectioSession(
        school_id="123",
        student_id="456789",
        cookies=[
            LectioCookie(
                name="ASP.NET_SessionId",
                value="synthetic-secret-cookie",
                domain="www.lectio.dk",
            )
        ],
    )


def make_lesson():
    return LectioLesson(
        id="lesson-1",
        start=datetime(2026, 9, 25, 8, 15, tzinfo=timezone.utc),
        end=datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc),
        subject="Matematik",
    )


class FakeLectioClient:
    def __init__(self):
        self.schedule_calls = 0
        self.assignments_calls = 0
        self.schedule_failure = None

    async def get_schedule(self, start, end):
        self.schedule_calls += 1
        if self.schedule_failure is not None:
            raise self.schedule_failure
        return [make_lesson()]

    async def get_assignments(self, start, end):
        self.assignments_calls += 1
        raise RuntimeError("upstream failure containing 456789")


def test_source_results_are_cached_until_the_freshness_window_expires(tmp_path):
    async def run():
        now = [datetime(2026, 9, 25, tzinfo=timezone.utc)]
        client = FakeLectioClient()
        session = make_session()
        service = LectioDataService(
            session_provider=lambda: session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
            ttl_seconds=60,
            clock=lambda: now[0],
        )
        await service.initialize()

        first = await service.get_source("schedule", START, END)
        second = await service.get_source("schedule", START, END)
        now[0] += timedelta(seconds=61)
        third = await service.get_source("schedule", START, END)

        return client, first, second, third

    client, first, second, third = asyncio.run(run())

    assert client.schedule_calls == 2
    assert [item.id for item in first[0]] == ["lesson-1"]
    assert second[1].state == "valid"
    assert third[1].state == "valid"


def test_one_source_failure_returns_its_stale_data_without_affecting_another(tmp_path):
    async def run():
        now = [datetime(2026, 9, 25, tzinfo=timezone.utc)]
        client = FakeLectioClient()
        session = make_session()
        service = LectioDataService(
            session_provider=lambda: session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
            ttl_seconds=1,
            clock=lambda: now[0],
        )
        await service.initialize()
        await service.get_source("schedule", START, END)
        now[0] += timedelta(seconds=2)
        client.schedule_failure = RuntimeError("failed request for student 456789")
        stale = await service.get_source("schedule", START, END)
        with pytest.raises(LectioSourceUnavailable):
            await service.get_source("assignments", START, END)
        return stale, service.statuses()

    stale, statuses = asyncio.run(run())

    assert [item.id for item in stale[0]] == ["lesson-1"]
    assert stale[1].state == "stale"
    assert stale[1].is_stale is True
    assert "456789" not in (stale[1].error or "")
    assert statuses["assignments"].state == "error"
    assert statuses["schedule"].state == "stale"


def test_persisted_last_good_source_data_survives_a_gateway_restart(tmp_path):
    async def run():
        cache_path = Path(tmp_path) / "lectio-cache.json"
        client = FakeLectioClient()
        session = make_session()
        first_service = LectioDataService(
            session_provider=lambda: session,
            cache_path=cache_path,
            client_factory=lambda session: client,
            ttl_seconds=0,
        )
        await first_service.initialize()
        await first_service.get_source("schedule", START, END)

        failing_client = FakeLectioClient()
        failing_client.schedule_failure = RuntimeError("upstream unavailable")
        restored_service = LectioDataService(
            session_provider=lambda: session,
            cache_path=cache_path,
            client_factory=lambda session: failing_client,
            ttl_seconds=0,
        )
        await restored_service.initialize()
        restored = await restored_service.get_source("schedule", START, END)
        return cache_path.read_text(encoding="utf-8"), restored

    cache_text, restored = asyncio.run(run())

    assert [item.id for item in restored[0]] == ["lesson-1"]
    assert restored[1].state == "stale"
    assert "456789" not in cache_text
    assert "synthetic-secret-cookie" not in cache_text
    assert stat.S_IMODE((Path(tmp_path) / "lectio-cache.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((Path(tmp_path) / "lectio-cache-key").stat().st_mode) == 0o600


def test_last_good_data_survives_login_refresh_for_the_same_student(tmp_path):
    async def run():
        cache_path = Path(tmp_path) / "lectio-cache.json"
        first_session = make_session()
        first_client = FakeLectioClient()
        first_service = LectioDataService(
            session_provider=lambda: first_session,
            cache_path=cache_path,
            client_factory=lambda session: first_client,
        )
        await first_service.initialize()
        await first_service.get_source("schedule", START, END)

        refreshed_session = AuthenticatedLectioSession(
            school_id="123",
            student_id="456789",
            cookies=[
                LectioCookie(
                    name="ASP.NET_SessionId",
                    value="refreshed-cookie-secret",
                    domain="www.lectio.dk",
                )
            ],
        )
        refreshed_client = FakeLectioClient()
        refreshed_service = LectioDataService(
            session_provider=lambda: refreshed_session,
            cache_path=cache_path,
            client_factory=lambda session: refreshed_client,
        )
        await refreshed_service.initialize()
        result = await refreshed_service.get_source("schedule", START, END)
        return refreshed_client, result

    client, result = asyncio.run(run())

    assert client.schedule_calls == 0
    assert [item.id for item in result[0]] == ["lesson-1"]
    assert result[1].state == "valid"


def test_expired_session_marks_auth_and_keeps_last_good_data(tmp_path):
    async def run():
        now = [datetime(2026, 9, 25, tzinfo=timezone.utc)]
        client = FakeLectioClient()
        manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://localhost:6080/vnc.html",
            login_url="https://www.lectio.dk/",
        )
        manager.session = make_session()
        manager._set_state(AuthState.AUTHENTICATED)
        service = LectioDataService(
            session_provider=lambda: manager.session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
            ttl_seconds=1,
            clock=lambda: now[0],
            on_session_expired=manager.mark_session_expired,
        )
        await service.initialize()
        await service.get_source("schedule", START, END)
        now[0] += timedelta(seconds=2)
        client.schedule_failure = LectioSessionExpired("Lectio session expired")
        stale = await service.get_source("schedule", START, END)
        return manager, stale

    manager, stale = asyncio.run(run())

    assert manager.status().state.value == "SESSION_EXPIRED"
    assert [item.id for item in stale[0]] == ["lesson-1"]
    assert stale[1].state == "expired"
    assert stale[1].is_stale is True


def test_expired_session_without_cache_returns_auth_required(tmp_path):
    async def run():
        client = FakeLectioClient()
        client.schedule_failure = LectioSessionExpired("private upstream details")
        manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://localhost:6080/vnc.html",
            login_url="https://www.lectio.dk/",
        )
        manager.session = make_session()
        manager._set_state(AuthState.AUTHENTICATED)
        service = LectioDataService(
            session_provider=lambda: manager.session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
            on_session_expired=manager.mark_session_expired,
        )
        await service.initialize()
        with pytest.raises(LectioAuthenticationRequired):
            await service.get_source("schedule", START, END)
        return manager

    manager = asyncio.run(run())

    assert manager.status().state.value == "SESSION_EXPIRED"
    assert "private upstream details" not in (manager.status().error or "")


def test_cache_is_not_reused_after_the_configured_student_changes(tmp_path):
    async def run():
        sessions = [make_session()]
        client = FakeLectioClient()
        service = LectioDataService(
            session_provider=lambda: sessions[0],
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
        )
        await service.initialize()
        await service.get_source("schedule", START, END)
        sessions[0] = sessions[0].model_copy(update={"student_id": "987654"})
        await service.get_source("schedule", START, END)
        return client

    client = asyncio.run(run())

    assert client.schedule_calls == 2


def test_logged_out_session_does_not_serve_cached_personal_data(tmp_path):
    async def run():
        sessions = [make_session()]
        client = FakeLectioClient()
        service = LectioDataService(
            session_provider=lambda: sessions[0],
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
        )
        await service.initialize()
        await service.get_source("schedule", START, END)
        sessions[0] = None
        with pytest.raises(LectioAuthenticationRequired):
            await service.get_source("schedule", START, END)
        return client

    client = asyncio.run(run())

    assert client.schedule_calls == 1


def test_student_specific_sources_require_a_configured_student_id(tmp_path):
    async def run():
        client = FakeLectioClient()
        session = make_session().model_copy(update={"student_id": None})
        service = LectioDataService(
            session_provider=lambda: session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda current_session: client,
        )
        await service.initialize()
        with pytest.raises(LectioStudentIdRequired):
            await service.get_source("assignments", START, END)
        return client, service.statuses()["assignments"]

    client, status = asyncio.run(run())

    assert client.assignments_calls == 0
    assert status.state == "error"
    assert status.is_stale is False


def test_missing_session_fails_without_calling_lectio(tmp_path):
    async def run():
        client = FakeLectioClient()
        service = LectioDataService(
            session_provider=lambda: None,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
        )
        await service.initialize()
        return await service.get_source("schedule", START, END)

    with pytest.raises(LectioAuthenticationRequired, match="authentication"):
        asyncio.run(run())


def test_source_failure_status_survives_restart_without_any_cached_items(tmp_path):
    async def run():
        cache_path = Path(tmp_path) / "lectio-cache.json"
        session = make_session()
        service = LectioDataService(
            session_provider=lambda: session,
            cache_path=cache_path,
            client_factory=lambda current_session: FakeLectioClient(),
        )
        await service.initialize()
        with pytest.raises(LectioSourceUnavailable):
            await service.get_source("assignments", START, END)

        restored = LectioDataService(
            session_provider=lambda: session,
            cache_path=cache_path,
            client_factory=lambda current_session: FakeLectioClient(),
        )
        await restored.initialize()
        return restored.statuses()["assignments"]

    status = asyncio.run(run())

    assert status.state == "error"
    assert status.last_attempt_at is not None


def test_expired_request_from_old_session_does_not_expire_a_new_session(tmp_path):
    async def run():
        manager = AuthManager(
            data_dir=Path(tmp_path),
            browser_url="http://browser:8765",
            browser_view_url="http://localhost:6080/vnc.html",
            login_url="https://www.lectio.dk/",
        )
        manager.session = make_session()
        manager._set_state(AuthState.AUTHENTICATED)

        class SwitchingClient:
            async def get_schedule(self, start, end):
                manager.session = make_session().model_copy(
                    update={"student_id": "987654"}
                )
                manager._set_state(AuthState.AUTHENTICATED)
                raise LectioSessionExpired("old session expired")

        service = LectioDataService(
            session_provider=lambda: manager.session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: SwitchingClient(),
            on_session_expired=manager.mark_session_expired,
        )
        await service.initialize()
        with pytest.raises(LectioSourceUnavailable, match="session changed"):
            await service.get_source("schedule", START, END)
        return manager

    manager = asyncio.run(run())

    assert manager.status().state == AuthState.AUTHENTICATED
