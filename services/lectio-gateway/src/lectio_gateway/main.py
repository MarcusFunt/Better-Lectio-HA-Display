from fastapi import FastAPI

app = FastAPI(title="Better Lectio Gateway")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "lectio-gateway"}
