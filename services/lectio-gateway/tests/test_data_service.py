import asyncio
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
    LectioSyncStatus,
)

START = datetime(2026, 9, 25, tzinfo=timezone.utc)
END = datetime(2026, 9, 26, tzinfo=timezone.utc)
COPENHAGEN = ZoneInfo("Europe/Copenhagen")


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
        self.schedule_week_calls = []
        self.assignments_calls = 0
        self.schedule_failure = None
        self.schedule_items = [make_lesson()]

    async def get_schedule(self, start, end):
        self.schedule_calls += 1
        await asyncio.sleep(0.01)
        if self.schedule_failure is not None:
            raise self.schedule_failure
        return self.schedule_items

    async def get_schedule_week(self, iso_year, iso_week):
        self.schedule_calls += 1
        self.schedule_week_calls.append((iso_year, iso_week))
        await asyncio.sleep(0.01)
        if self.schedule_failure is not None:
            raise self.schedule_failure
        return self.schedule_items

    async def get_assignments(self, start, end):
        self.assignments_calls += 1
        raise RuntimeError("upstream failure containing 456789")


class _FailingScheduleClient:
    async def get_schedule(self, start, end):
        raise RuntimeError("schedule unavailable")

    async def get_schedule_week(self, iso_year, iso_week):
        raise RuntimeError("schedule unavailable")


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


def test_schedule_pages_are_reused_across_minute_shifted_ranges(tmp_path):
    async def run():
        client = FakeLectioClient()
        service = LectioDataService(
            session_provider=make_session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
        )
        await service.initialize()

        first = await service.get_source("schedule", START, END)
        second = await service.get_source(
            "schedule", START + timedelta(minutes=5), END + timedelta(minutes=5)
        )
        return client, first, second

    client, first, second = asyncio.run(run())

    assert client.schedule_calls == 1
    assert client.schedule_week_calls == [(2026, 39)]
    assert [item.id for item in first[0]] == ["lesson-1"]
    assert [item.id for item in second[0]] == ["lesson-1"]


def test_concurrent_requests_share_one_schedule_week_fetch(tmp_path):
    async def run():
        client = FakeLectioClient()
        service = LectioDataService(
            session_provider=make_session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
        )
        await service.initialize()
        results = await asyncio.gather(
            service.get_source("schedule", START, END),
            service.get_source(
                "schedule", START + timedelta(minutes=5), END + timedelta(minutes=5)
            ),
        )
        return client, results

    client, results = asyncio.run(run())

    assert client.schedule_calls == 1
    assert [item.id for item in results[0][0]] == ["lesson-1"]
    assert [item.id for item in results[1][0]] == ["lesson-1"]


def test_week_cache_is_isolated_by_owner(tmp_path):
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

    assert client.schedule_week_calls == [(2026, 39), (2026, 39)]


def test_successfully_empty_week_is_reused_after_range_shift(tmp_path):
    async def run():
        client = FakeLectioClient()
        client.schedule_items = []
        service = LectioDataService(
            session_provider=make_session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
        )
        await service.initialize()
        first = await service.get_source("schedule", START, END)
        second = await service.get_source(
            "schedule", START + timedelta(minutes=5), END + timedelta(minutes=5)
        )
        return client, first, second

    client, first, second = asyncio.run(run())

    assert client.schedule_calls == 1
    assert first[0] == second[0] == []
    assert first[1].state == second[1].state == "valid"


def test_failed_week_refresh_uses_lkg_for_a_shifted_range(tmp_path):
    async def run():
        now = [datetime(2026, 9, 25, tzinfo=timezone.utc)]
        client = FakeLectioClient()
        service = LectioDataService(
            session_provider=make_session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
            ttl_seconds=60,
            clock=lambda: now[0],
        )
        await service.initialize()
        await service.get_source("schedule", START, END)
        now[0] += timedelta(seconds=61)
        client.schedule_failure = RuntimeError("temporary upstream failure")
        stale = await service.get_source(
            "schedule", START + timedelta(minutes=5), END + timedelta(minutes=5)
        )
        return client, stale

    client, stale = asyncio.run(run())

    assert client.schedule_calls == 2
    assert [item.id for item in stale[0]] == ["lesson-1"]
    assert stale[1].state == "stale"
    assert stale[1].is_stale is True


def test_client_factory_failure_keeps_week_lkg(tmp_path):
    async def run():
        now = [datetime(2026, 9, 25, tzinfo=timezone.utc)]
        fail_factory = [False]
        client = FakeLectioClient()

        def factory(session):
            if fail_factory[0]:
                raise RuntimeError("client setup failed")
            return client

        service = LectioDataService(
            session_provider=make_session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=factory,
            ttl_seconds=60,
            clock=lambda: now[0],
        )
        await service.initialize()
        await service.get_source("schedule", START, END)
        now[0] += timedelta(seconds=61)
        fail_factory[0] = True
        stale = await service.get_source(
            "schedule", START + timedelta(minutes=5), END + timedelta(minutes=5)
        )
        return stale

    stale = asyncio.run(run())

    assert [item.id for item in stale[0]] == ["lesson-1"]
    assert stale[1].state == "stale"


def test_week_resolution_uses_copenhagen_bounds_across_dst(tmp_path):
    async def run():
        client = FakeLectioClient()
        service = LectioDataService(
            session_provider=make_session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
        )
        await service.initialize()
        start = datetime.fromisoformat("2026-03-29T01:30:00+01:00")
        exact_monday = datetime.fromisoformat("2026-03-30T00:00:00+02:00")
        await service.get_source("schedule", start, exact_monday)
        await service.get_source("schedule", start, exact_monday + timedelta(minutes=1))
        return client

    client = asyncio.run(run())

    assert client.schedule_week_calls == [(2026, 13), (2026, 14)]


def test_week_resolution_crosses_iso_year_boundary(tmp_path):
    async def run():
        client = FakeLectioClient()
        service = LectioDataService(
            session_provider=make_session,
            cache_path=Path(tmp_path) / "lectio-cache.json",
            client_factory=lambda session: client,
        )
        await service.initialize()
        sunday = datetime.fromisoformat("2027-01-03T12:00:00+01:00")
        exact_monday = datetime.fromisoformat("2027-01-04T00:00:00+01:00")
        await service.get_source("schedule", sunday, exact_monday)
        await service.get_source("schedule", sunday, exact_monday + timedelta(minutes=1))
        return client

    client = asyncio.run(run())

    assert client.schedule_week_calls == [(2026, 53), (2027, 1)]


def test_legacy_range_entry_is_fallback_only_when_it_covers_full_week(tmp_path):
    async def seed_range(cache_path, start, end):
        session = make_session()
        initializer = LectioDataService(
            session_provider=lambda: session,
            cache_path=cache_path,
            client_factory=lambda current_session: FakeLectioClient(),
        )
        await initializer.initialize()
        owner = initializer._owner_scope(session)
        status = LectioSyncStatus(
            state="valid",
            last_attempt_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
            last_successful_sync=datetime(2026, 9, 25, tzinfo=timezone.utc),
        )
        cache_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": [
                        {
                            "source": "schedule",
                            "owner": owner,
                            "start": start.isoformat(),
                            "end": end.isoformat(),
                            "items": [make_lesson().model_dump(mode="json")],
                            "status": status.model_dump(mode="json"),
                        }
                    ],
                    "statuses": [],
                }
            ),
            encoding="utf-8",
        )
        return session

    async def run():
        full_path = Path(tmp_path) / "full-week-cache.json"
        week_start = datetime(2026, 9, 21, tzinfo=COPENHAGEN)
        week_end = datetime(2026, 9, 28, tzinfo=COPENHAGEN)
        full_session = await seed_range(full_path, week_start, week_end)
        full_service = LectioDataService(
            session_provider=lambda: full_session,
            cache_path=full_path,
            client_factory=lambda session: _FailingScheduleClient(),
        )
        await full_service.initialize()
        full_week_fallback = await full_service.get_source(
            "schedule", START, END
        )

        partial_path = Path(tmp_path) / "partial-week-cache.json"
        partial_start = week_start
        partial_end = week_start + timedelta(days=2)
        partial_session = await seed_range(partial_path, partial_start, partial_end)
        partial_service = LectioDataService(
            session_provider=lambda: partial_session,
            cache_path=partial_path,
            client_factory=lambda session: _FailingScheduleClient(),
        )
        await partial_service.initialize()
        with pytest.raises(LectioSourceUnavailable):
            await partial_service.get_source(
                "schedule",
                partial_start + timedelta(days=1),
                partial_end + timedelta(days=1),
            )
        return full_week_fallback

    full_week_fallback = asyncio.run(run())

    assert [item.id for item in full_week_fallback[0]] == ["lesson-1"]
    assert full_week_fallback[1].state == "stale"


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
            async def get_schedule_week(self, iso_year, iso_week):
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
