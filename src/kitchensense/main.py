from fastapi import FastAPI

app = FastAPI(title="KitchenSense")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/deep")
async def health_deep() -> dict[str, str]:
    # Later: check database, storage, queue, AI services.
    return {"status": "ok", "database": "not_configured"}

@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "KitchenSense", "docs": "/docs"}