"""Same-origin FastAPI host for the OPS Mini App."""

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.telegram_auth import require_telegram_access
from application.flight_views import (
    CrewPreparation,
    FlightCard,
    FlightNotFound,
    FlightViews,
    NotamPreparation,
    Schedule,
    ScheduleUnavailable,
    WeatherPreparation,
)
from config.paths import project_directory

FlightId = Annotated[int, Path(gt=0)]


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

    @app.exception_handler(ScheduleUnavailable)
    async def schedule_unavailable(_request, _error):
        return JSONResponse(
            {"detail": "Leon schedule unavailable. Try again."}, status_code=502
        )

    @router.get("/schedule")
    async def schedule(day: date | None = None) -> Schedule:
        return await views.schedule(day or datetime.now(UTC).date())

    @router.get("/flights/{flight_id}")
    async def card(flight_id: FlightId, day: date) -> FlightCard:
        return await views.card(day, flight_id)

    @router.get("/flights/{flight_id}/preparation/crew")
    async def crew(flight_id: FlightId, day: date) -> CrewPreparation:
        return await views.crew(day, flight_id)

    @router.get("/flights/{flight_id}/preparation/weather")
    async def weather(flight_id: FlightId, day: date) -> WeatherPreparation:
        return await views.weather(day, flight_id)

    @router.get("/flights/{flight_id}/preparation/notams")
    async def notams(flight_id: FlightId, day: date) -> NotamPreparation:
        return await views.notams(day, flight_id)

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
