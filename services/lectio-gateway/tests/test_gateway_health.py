import asyncio

from httpx import ASGITransport, AsyncClient
from lectio_gateway.main import app


def test_health_endpoint_reports_gateway_identity():
    async def request_health():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/health")

    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "lectio-gateway"}
