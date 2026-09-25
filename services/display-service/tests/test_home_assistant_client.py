import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web
from display_service.ha_client import HomeAssistantApiError, HomeAssistantClient


def test_client_uses_authenticated_calendar_todo_and_sensor_apis():
    async def run():
        seen = []

        async def handle(request):
            body = await request.json() if request.can_read_body else None
            seen.append((request.method, request.path, dict(request.query), body, request.headers.get("Authorization")))
            if request.path == "/api/services/calendar/get_events":
                payload = {
                    "service_response": {
                        "calendar.lectio": {
                            "events": [
                                {
                                    "summary": "Physics",
                                    "start": "2026-09-25T08:00:00+02:00",
                                    "end": "2026-09-25T08:45:00+02:00",
                                }
                            ]
                        }
                    }
                }
            elif request.path == "/api/services/todo/get_items":
                payload = {
                    "service_response": {
                        "todo.lectio_assignments": {
                            "items": [{"summary": "Essay", "uid": "task-1", "status": "needs_action"}]
                        }
                    }
                }
            else:
                payload = {
                    "entity_id": request.match_info["path"].split("/")[-1],
                    "state": "1",
                    "attributes": (
                        {"cancellations": [{"id": "cancel-1"}], "sync": {"state": "valid"}}
                        if request.path.endswith("sensor.lectio_cancellations")
                        else {
                            "sync": {
                                "state": "stale",
                                "is_stale": True,
                                "last_successful_sync": "2026-09-25T06:00:00+00:00",
                            }
                        }
                    ),
                }
            return web.json_response(payload)

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        start = datetime(2026, 9, 25, 0, 0, tzinfo=ZoneInfo("Europe/Copenhagen"))
        end = datetime(2026, 9, 28, 0, 0, tzinfo=ZoneInfo("Europe/Copenhagen"))

        try:
            async with HomeAssistantClient(
                f"http://127.0.0.1:{port}", "test-long-lived-token"
            ) as client:
                events = await client.async_get_calendar_events("calendar.lectio", start, end)
                todo_items = await client.async_get_todo_items("todo.lectio_assignments")
                cancellations = await client.async_get_cancellations("sensor.lectio_cancellations")
                sync = await client.async_get_entity_sync("todo.lectio_homework")

            assert events[0]["summary"] == "Physics"
            assert todo_items[0]["uid"] == "task-1"
            assert cancellations == [{"id": "cancel-1"}]
            assert sync["state"] == "stale"
            assert sync["is_stale"] is True
            assert seen[0][0:2] == ("POST", "/api/services/calendar/get_events")
            assert "return_response" in seen[0][2]
            assert seen[0][3] == {
                "entity_id": "calendar.lectio",
                "start_date_time": start.isoformat(),
                "end_date_time": end.isoformat(),
            }
            assert seen[1][3] == {
                "entity_id": "todo.lectio_assignments",
                "status": "needs_action",
            }
            assert seen[2][0:2] == ("GET", "/api/states/sensor.lectio_cancellations")
            assert seen[3][0:2] == ("GET", "/api/states/todo.lectio_homework")
            assert all(request[4] == "Bearer test-long-lived-token" for request in seen)
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_client_rejects_credentials_in_url_and_missing_token():
    for url, token in [
        ("http://user:password@ha.local", "token"),
        ("http://ha.local?token=secret", "token"),
        ("http://ha.local", ""),
    ]:
        try:
            HomeAssistantClient(url, token)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid Home Assistant connection was accepted")


def test_http_errors_are_sanitized_and_classified():
    async def run():
        async def unauthorized(request):
            return web.json_response({"message": "test-long-lived-token must not leak"}, status=401)

        app = web.Application()
        app.router.add_get("/api/states/{entity_id}", unauthorized)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        try:
            async with HomeAssistantClient(
                f"http://127.0.0.1:{port}", "test-long-lived-token"
            ) as client:
                try:
                    await client.async_get_cancellations("sensor.lectio_cancellations")
                except HomeAssistantApiError as error:
                    assert error.code == "unauthorized"
                    assert "test-long-lived-token" not in str(error)
                else:
                    raise AssertionError("unauthorized request was accepted")
        finally:
            await runner.cleanup()

    asyncio.run(run())
