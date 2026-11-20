"""Same-origin FastAPI host for the OPS Mini App."""

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.ops_routes import create_ops_router
from api.telegram_auth import require_telegram_access
from application.flight_views import (
    CrewPreparation,
    FlightCard,
    FlightChanged,
    FlightNotFound,
    FlightViews,
    Handling,
    NotamPreparation,
    Plan,
    Schedule,
    ScheduleUnavailable,
    WeatherPreparation,
)
from config.paths import project_directory

FlightId = Annotated[int, Path(gt=0)]


def flight_window(
    day: date,
    week: date | None = None,
    version: Annotated[str | None, Query(pattern=r"^[0-9a-f]{24}$")] = None,
):
    if week and not 0 <= (day - week).days <= 6:
        raise HTTPException(422, "Flight date must be in the selected SKED week.")
    if week and date.max.toordinal() - week.toordinal() < 6:
        raise HTTPException(422, "Invalid SKED week.")
    return day, {"week": week, "version": version}


Selection = Annotated[tuple, Depends(flight_window)]


def create_app():
    app = FastAPI(
        title="OPS API", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json"
    )
    router = APIRouter(
        prefix="/api/v1", dependencies=[Depends(require_telegram_access)]
    )
    views = FlightViews()

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(FlightNotFound)
    async def missing_flight(_request, _error):
        return JSONResponse({"detail": "Flight no longer in SKED."}, status_code=404)

    @app.exception_handler(FlightChanged)
    async def changed_flight(_request, _error):
        return JSONResponse(
            {"detail": "Flight changed. Return to SKED and refresh."}, status_code=409
        )

    @app.exception_handler(ScheduleUnavailable)
    async def schedule_unavailable(_request, _error):
        return JSONResponse(
            {"detail": "Leon schedule unavailable. Try again."}, status_code=502
        )

    @router.get("/schedule")
    async def schedule(
        day: date | None = None, end_day: date | None = None, refresh: bool = False
    ) -> Schedule:
        start = day or datetime.now(UTC).date()
        if end_day and not 0 <= (end_day - start).days <= 6:
            raise HTTPException(422, "SKED period must cover one to seven UTC days.")
        return await views.schedule(start, end_day, refresh=refresh)

    @router.get("/flights/{flight_id}")
    async def card(flight_id: FlightId, selection: Selection) -> FlightCard:
        day, options = selection
        return await views.card(day, flight_id, **options)

    @router.get("/flights/{flight_id}/plan")
    async def plan(
        flight_id: FlightId, selection: Selection, refresh: bool = False
    ) -> Plan:
        day, options = selection
        return await views.plan(day, flight_id, refresh=refresh, **options)

    @router.get("/flights/{flight_id}/handling")
    async def handling(
        flight_id: FlightId, selection: Selection, refresh: bool = False
    ) -> Handling:
        day, options = selection
        return await views.handling(day, flight_id, refresh=refresh, **options)

    @router.get("/flights/{flight_id}/weather")
    async def metar(
        flight_id: FlightId, selection: Selection, refresh: bool = False
    ) -> WeatherPreparation:
        day, options = selection
        return await views.weather(
            day, flight_id, include_taf=False, refresh=refresh, **options
        )

    @router.get("/flights/{flight_id}/preparation/crew")
    async def crew(
        flight_id: FlightId,
        selection: Selection,
        refresh: bool = False,
        assignments_only: bool = False,
    ) -> CrewPreparation:
        day, options = selection
        return await views.crew(
            day,
            flight_id,
            refresh=refresh,
            assignments_only=assignments_only,
            **options,
        )

    @router.get("/flights/{flight_id}/preparation/weather")
    async def weather(
        flight_id: FlightId, selection: Selection, refresh: bool = False
    ) -> WeatherPreparation:
        day, options = selection
        return await views.weather(day, flight_id, refresh=refresh, **options)

    @router.get("/flights/{flight_id}/preparation/notams")
    async def notams(
        flight_id: FlightId, selection: Selection, refresh: bool = False
    ) -> NotamPreparation:
        day, options = selection
        return await views.notams(day, flight_id, refresh=refresh, **options)

    router.include_router(create_ops_router(views, flight_window))
    app.include_router(router)

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    dist = project_directory() / "frontend" / "dist"
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets))

    @app.get("/", include_in_schema=False)
    async def frontend():
        index = dist / "index.html"
        if not index.is_file():
            raise HTTPException(503, "Build the Mini App frontend first.")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
