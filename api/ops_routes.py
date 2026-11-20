"""Authenticated HTTP entry points for the OPS dashboard tools."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from api.telegram_auth import require_telegram_access
from application.ops_views import OpsViews, source_state
from services.analytics_ai import prioritize_metrics
from services.analytics_insights import build_insights
from services.charter_intelligence import (
    CHECKLIST_TOOLS,
    charter_request_sources,
    load_flight_checklist,
)
from services.charter_recommendations import charter_priorities
from services.fleet_operations import charter_sales_access
from services.ground_turns import KINDS, ground_turns

Actor = Annotated[int, Depends(require_telegram_access)]
PositiveId = Annotated[int, Path(gt=0, lt=2**63)]
TurnKey = Annotated[str, Path(pattern=r"^[A-Z0-9]{4}:[io]:[0-9]{1,19}$")]
Station = Annotated[str, Path(pattern=r"^[A-Z0-9]{4}$")]
AirportFilter = Annotated[str | None, Query(pattern=r"^[A-Z0-9]{4}$")]
TypeFilter = Annotated[str | None, Query(pattern=r"^[A-Z0-9]{2,4}$")]


class GroundEvent(BaseModel):
    request_id: UUID
    kind: str
    event_at: AwareDatetime | None = None
    target_at: AwareDatetime | None = None
    expected_at: AwareDatetime | None = None
    process: (
        Literal["fuel", "handling", "cabin", "pax", "documents", "other"] | None
    ) = None
    text: Annotated[str, Field(max_length=500)] = ""
    critical: bool = False

    @model_validator(mode="after")
    def valid_event(self):
        if self.kind not in KINDS:
            raise ValueError("Unknown ground event.")
        if self.kind in {"BLOCKER_OPEN", "BLOCKER_CLOSE"} and self.process is None:
            raise ValueError("Select a blocked process.")
        if self.kind == "BLOCKER_OPEN" and not 1 <= len(self.text.strip()) <= 160:
            raise ValueError("Enter a blocker reason of up to 160 characters.")
        if self.kind == "TARGET" and self.target_at is None:
            raise ValueError("Enter a UTC target.")
        if self.kind in {"NOTE", "STAND"} and not self.text.strip():
            raise ValueError("Enter details.")
        return self

    def details(self):
        if self.kind == "TARGET":
            return {"target_at": self.target_at}
        if self.kind in {"BLOCKER_OPEN", "BLOCKER_CLOSE"}:
            detail = {"process": self.process}
            if self.kind == "BLOCKER_OPEN":
                detail.update(expected_at=self.expected_at, reason=self.text.strip())
            return detail
        if self.kind == "STAND":
            return {"stand": self.text.strip()}
        if self.kind == "NOTE":
            return {"text": self.text.strip(), "critical": self.critical}
        return {}


def create_ops_router(flights, flight_window):
    router = APIRouter()
    views = OpsViews()
    Selection = Annotated[tuple, Depends(flight_window)]

    async def resolve_turn(key, *, refresh=False):
        state = await views.ground(refresh=refresh)
        if state["status"] == "UNAVAILABLE":
            raise HTTPException(502, "Leon Ground Status unavailable. Try again.")
        turn = next((item for item in state["cards"] if item["key"] == key), None)
        if turn is None:
            if state["status"] == "PARTIAL":
                raise HTTPException(502, "Turn missing from the partial Leon schedule.")
            raise HTTPException(404, "Turn is outside the Ground Status window.")
        return turn, state

    @router.get("/ground")
    async def ground(refresh: bool = False):
        return await views.ground(refresh=refresh)

    @router.get("/ground/turns/{key}")
    async def turn_detail(key: TurnKey, refresh: bool = False):
        turn, state = await resolve_turn(key, refresh=refresh)
        return await views.ground_detail(turn, state)

    @router.post("/ground/turns/{key}/events")
    async def record_event(key: TurnKey, event: GroundEvent, actor: Actor):
        now = datetime.now(UTC)
        when = event.event_at or now
        if not now - timedelta(hours=24) <= when <= now:
            raise HTTPException(422, "Event UTC must be within the last 24 hours.")
        if (
            event.kind == "BLOCKER_OPEN"
            and event.expected_at is not None
            and event.expected_at <= now
        ):
            raise HTTPException(422, "Expected clear UTC must be in the future.")
        turn, state = await resolve_turn(key)
        if not turn["outbound_id"] and event.kind in {"OUT_BLOCK", "TARGET"}:
            raise HTTPException(422, "This turn has no verified outbound leg.")
        try:
            await asyncio.to_thread(
                ground_turns.record,
                turn,
                event.kind,
                actor_id=actor,
                event_at=when,
                request_id=f"miniapp:{actor}:{event.request_id}",
                detail=event.details(),
            )
            updated = await views.ground()
            current = next(
                (item for item in updated["cards"] if item["key"] == key), turn
            )
            return await views.ground_detail(current, state)
        except ValueError:
            raise HTTPException(
                422, "Invalid ground event. Check the details."
            ) from None
        except (OSError, sqlite3.Error):
            raise HTTPException(
                503, "OPS journal unavailable. Retry this event."
            ) from None

    @router.get("/ground/stations/{station}")
    async def station_report(station: Station):
        return await views.station(station)

    @router.get("/ground/turns/{key}/history")
    async def ground_history(key: TurnKey):
        turn, _ = await resolve_turn(key)
        return await views.ground_history(turn)

    @router.get("/ops-brief")
    async def ops_brief(actor: Actor, refresh: bool = False):
        return await views.fleet(True, actor, refresh=refresh)

    @router.get("/fleet/aircraft")
    async def aircraft(refresh: bool = False):
        return await views.aircraft(refresh=refresh)

    @router.get("/fleet/aircraft/{aircraft_id}/documents")
    async def aircraft_documents(aircraft_id: PositiveId, refresh: bool = False):
        result = await views.documents(aircraft_id, refresh=refresh)
        if "aircraft" in result and result["aircraft"] is None:
            raise HTTPException(404, "Aircraft is no longer in the Leon fleet.")
        return result

    @router.get("/fleet/operator")
    async def operator(refresh: bool = False):
        return await views.operator(refresh=refresh)

    @router.get("/flights/{flight_id}/checklist")
    async def checklist(
        flight_id: PositiveId, selection: Selection, refresh: bool = False
    ):
        day, options = selection
        leg = await flights.selected_leg(day, flight_id, **options)

        async def load():
            data = await load_flight_checklist(
                CHECKLIST_TOOLS["OPS"], leg.id, limit=100
            )
            return {**data, "complete": len(data["items"]) == data["count"]}

        return await views.read(
            ("flight checklist", leg.id, leg.version), 60, load, refresh=refresh
        )

    @router.get("/analytics")
    async def analytics(
        period: Literal["7d", "1m", "3m", "6m", "12m"] = "7d",
        refresh: bool = False,
    ):
        return await views.analytics(period, refresh=refresh)

    @router.get("/analytics/journey")
    async def journey_history(
        days: int = 7,
        departure: AirportFilter = None,
        arrival: AirportFilter = None,
        aircraft_type: TypeFilter = None,
        refresh: bool = False,
    ):
        if days not in {7, 30, 90, 365}:
            raise HTTPException(422, "Select a 7, 30, 90 or 365-day period.")
        if bool(departure) != bool(arrival):
            raise HTTPException(422, "Enter both DEP and ARR to filter a route.")
        return await views.journey(
            days, departure, arrival, aircraft_type, refresh=refresh
        )

    @router.get("/analytics/journey/priorities")
    async def analytics_priorities(
        actor: Actor,
        days: int = 7,
        departure: AirportFilter = None,
        arrival: AirportFilter = None,
        aircraft_type: TypeFilter = None,
        charter: bool = False,
    ):
        data = await journey_history(days, departure, arrival, aircraft_type)
        if data["status"] == "UNAVAILABLE":
            return data
        if charter:
            priorities = await charter_priorities(
                {"LEON_JOURNEY_LOG": data}, ("charter", "history", actor, days)
            )
        else:
            priorities = await prioritize_metrics(
                build_insights(data), ("analytics", actor, days)
            )
        return {
            **source_state(datetime.now(UTC), 90, complete=data["status"] == "READY"),
            "priorities": priorities,
        }

    @router.get("/charter/fleet")
    async def charter_fleet(actor: Actor, refresh: bool = False):
        result = await views.fleet(False, actor, refresh=refresh)
        return {**result, "sales_access": charter_sales_access(actor)}

    @router.get("/charter/sales")
    async def charter_sales(actor: Actor, refresh: bool = False):
        if not charter_sales_access(actor):
            raise HTTPException(403, "Charter Sales access is required.")
        return await views.charter_sales(refresh=refresh)

    @router.get("/charter/sales/{rfq_id}")
    async def charter_request(rfq_id: PositiveId, actor: Actor, refresh: bool = False):
        if not charter_sales_access(actor):
            raise HTTPException(403, "Charter Sales access is required.")
        sales = await views.charter_sales()
        if sales["status"] == "UNAVAILABLE":
            raise HTTPException(502, "Leon Sales unavailable. Try again.")
        if not any(request["id"] == rfq_id for request in sales["requests"]):
            raise HTTPException(404, "Quote request is outside the current Sales list.")
        return await views.charter_sales(rfq_id, refresh=refresh)

    @router.get("/charter/sales/{rfq_id}/priorities")
    async def request_priorities(rfq_id: PositiveId, actor: Actor):
        data = await charter_request(rfq_id, actor)
        if data["status"] == "UNAVAILABLE":
            return data
        priorities = await charter_priorities(
            charter_request_sources(data), ("charter", "rfq", actor, rfq_id)
        )
        return {**source_state(datetime.now(UTC), 90), "priorities": priorities}

    return router
