"""Liveness, readiness and the deep check."""

from __future__ import annotations

from fastapi import APIRouter

from kitchensense.api.dependencies import DatabaseDep
from kitchensense.api.schemas import DeepHealthResponse, HealthResponse, RootResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness")
async def health() -> HealthResponse:
    """Is the process up. Nothing else.

    The container app's liveness and readiness probes point here, so this must
    not touch the database: PostgreSQL is stopped outside working hours, and a
    probe that failed with it would have Azure restarting a perfectly healthy
    container all evening.
    """
    return HealthResponse(status="ok")


@router.get("/health/deep", response_model=DeepHealthResponse, summary="Dependencies")
async def health_deep(database: DatabaseDep) -> DeepHealthResponse:
    """Reports whether the database is reachable, and answers 200 either way.

    ``status`` describes the application, ``database`` describes a dependency,
    and they are separate on purpose. The app really is fine with the database
    stopped — it starts, serves ``/health``, and returns 503 only from the
    endpoints that need rows. Collapsing that into one red light would hide
    the distinction from whoever is reading it at 08:00.
    """
    status = await database.check()
    return DeepHealthResponse(
        status="ok",
        database=status.reachability,
        database_url_source=status.source,
        detail=status.detail,
    )


@router.get("/", response_model=RootResponse, include_in_schema=False)
async def root() -> RootResponse:
    return RootResponse(service="KitchenSense", docs="/docs")
