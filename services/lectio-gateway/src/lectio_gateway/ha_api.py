"""Token-protected, read-only API surface for Home Assistant hosts."""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .ha_api_auth import GatewayApiTokenStore

_ALLOWED_ENDPOINTS = {"status", "schedule", "assignments", "homework", "cancellations"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the private gateway client for the proxy process."""
    gateway_url = os.getenv(
        "LECTIO_HA_API_GATEWAY_URL", "http://lectio-gateway:8000"
    ).rstrip("/")
    app.state.api_token = os.getenv("LECTIO_HA_API_TOKEN", "").strip()
    app.state.api_token_store = GatewayApiTokenStore(
        Path(os.getenv("LECTIO_HA_API_AUTH_DIR", "/var/lib/better-lectio-api-auth"))
    )
    app.state.gateway_client = httpx.AsyncClient(
        base_url=gateway_url,
        timeout=httpx.Timeout(15.0),
    )
    try:
        yield
    finally:
        await app.state.gateway_client.aclose()


app = FastAPI(title="Better Lectio Home Assistant API", lifespan=lifespan)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Report process health without exposing configuration or credentials."""
    return {"status": "ok", "service": "lectio-ha-api"}


@app.get("/api/v1/{endpoint}")
async def api_endpoint(endpoint: str, request: Request) -> JSONResponse:
    """Forward only the explicitly allowed read-only gateway endpoints."""
    if endpoint not in _ALLOWED_ENDPOINTS:
        raise HTTPException(status_code=404, detail="API endpoint not found")

    expected_token = getattr(request.app.state, "api_token", "")
    token_store = getattr(request.app.state, "api_token_store", None)
    managed_token = bool(token_store and token_store.managed_digest_exists)
    if not managed_token and not expected_token:
        raise HTTPException(
            status_code=503, detail="Home Assistant API access is not configured"
        )
    authorization = request.headers.get("authorization", "").strip().split(None, 1)
    supplied_token = (
        authorization[1]
        if len(authorization) == 2
        and authorization[0].casefold() == "bearer"
        else ""
    )
    if not supplied_token or len(supplied_token) > 512:
        is_valid = False
    elif managed_token:
        is_valid = token_store.matches(supplied_token)
    else:
        is_valid = hmac.compare_digest(supplied_token, expected_token)
    if not is_valid:
        raise HTTPException(
            status_code=401,
            detail="Invalid Home Assistant API credential",
            headers={"WWW-Authenticate": "Bearer"},
        )

    query_items = list(request.query_params.multi_items())
    if endpoint == "status":
        if query_items:
            raise HTTPException(status_code=422, detail="Status does not accept query parameters")
        params = None
    else:
        if len(query_items) != 2 or {key for key, _ in query_items} != {"start", "end"}:
            raise HTTPException(
                status_code=422,
                detail="Provide exactly one start and end date range.",
            )
        params = query_items

    try:
        response = await request.app.state.gateway_client.get(
            f"/api/v1/{endpoint}", params=params
        )
        payload = response.json()
    except (httpx.RequestError, ValueError) as err:
        raise HTTPException(
            status_code=502, detail="Lectio Gateway is unavailable"
        ) from err
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Lectio Gateway returned an invalid response")
    return JSONResponse(status_code=response.status_code, content=payload)
