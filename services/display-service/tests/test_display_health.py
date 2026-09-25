import asyncio

from display_service.main import app
from httpx import ASGITransport, AsyncClient


def test_health_endpoint_reports_display_service_identity():
    async def request_health():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/health")

    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "display-service"}
