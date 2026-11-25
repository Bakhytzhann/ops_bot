import asyncio
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from api.telegram_auth import require_telegram_access
from config.settings import settings
from services.ai_analysis import AnalysisUnavailable
from services.charter_brief import refine_offer_brief
from services.charter_input import (
    CharterDeskRequest,
    CharterIntent,
    parse_charter_request,
)
from services.charter_rates import AirportFeeInput, CharterRateInput, charter_rates
from services.fleet_operations import charter_sales_access

Actor = Annotated[int, Depends(require_telegram_access)]


class CharterText(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=2600)


class CharterConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: CharterDeskRequest


class CharterBriefInstruction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    current_brief: str = Field(min_length=1, max_length=3200)
    instruction: str = Field(min_length=1, max_length=600)


def _require_sales(actor):
    if not charter_sales_access(actor):
        raise HTTPException(403, "Charter Sales access is required.")


def create_charter_router(views):
    router = APIRouter(prefix="/charter")

    async def load_rates(actor):
        try:
            data = await asyncio.to_thread(charter_rates.snapshot)
        except (OSError, sqlite3.Error):
            raise HTTPException(
                503, "Rate Cards are temporarily unavailable."
            ) from None
        return {**data, "can_edit": actor in settings.admin_ids}

    @router.get("/access")
    async def access(actor: Actor):
        return {
            "sales_access": charter_sales_access(actor),
            "edit_rates": actor in settings.admin_ids,
        }

    @router.get("/current-fleet")
    async def current_fleet(actor: Actor, refresh: bool = False):
        _require_sales(actor)
        try:
            return await views.charter_current_fleet(refresh=refresh)
        except ConnectionError as exc:
            raise HTTPException(502, str(exc)) from None

    @router.post("/parse", response_model=CharterIntent)
    async def parse(payload: CharterText, actor: Actor):
        _require_sales(actor)
        try:
            return await parse_charter_request(payload.text, actor)
        except AnalysisUnavailable:
            raise HTTPException(
                503, "Qwen could not parse the request. Try again."
            ) from None

    @router.post("/matrix")
    async def matrix(payload: CharterConfirmation, actor: Actor):
        _require_sales(actor)
        try:
            return await views.charter_matrix(payload.request.model_dump(mode="json"))
        except ConnectionError as exc:
            raise HTTPException(502, str(exc)) from None
        except (OSError, sqlite3.Error):
            raise HTTPException(
                503, "Charter data are temporarily unavailable."
            ) from None

    @router.post("/brief/refine")
    async def refine(payload: CharterBriefInstruction, actor: Actor):
        _require_sales(actor)
        try:
            brief = await refine_offer_brief(
                payload.current_brief, payload.instruction, actor
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except AnalysisUnavailable:
            raise HTTPException(
                503, "Qwen could not edit the Offer Brief. Try again."
            ) from None
        return {"brief": brief}

    @router.get("/opportunities")
    async def opportunities(actor: Actor, refresh: bool = False):
        return await views.charter_opportunities(refresh=refresh)

    @router.get("/rates")
    async def rates(actor: Actor):
        _require_sales(actor)
        return await load_rates(actor)

    @router.post("/rates")
    async def save_rate(payload: CharterRateInput | AirportFeeInput, actor: Actor):
        _require_sales(actor)
        if actor not in settings.admin_ids:
            raise HTTPException(403, "Rate Card editing requires administrator access.")
        save = (
            charter_rates.save_airport_profile
            if isinstance(payload, AirportFeeInput)
            else charter_rates.save_rate
        )
        try:
            await asyncio.to_thread(save, payload.model_dump(), actor)
        except (OSError, sqlite3.Error):
            raise HTTPException(503, "Could not save the new rate version.") from None
        return await load_rates(actor)

    return router
