import asyncio
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from loguru import logger

from config.settings import settings

BASE_URL = "https://public-api.foreflight.com/public/api"
DOCUMENTS = frozenset({"briefing", "navlog", "icao", "overflightreport", "wb", "rwa"})
ROUTE_FORMAT = re.compile(r"""[A-Z0-9 /+\-°:.,′’"″”']+""")
SPEED_LEVEL = re.compile(r"[KMN]\d{3,4}(?:[AFMS]\d{3,4}|VFR)")


def enroute_text(value, departure, arrival):
    if not isinstance(value, str):
        return None
    tokens = value.upper().split()
    if tokens and tokens[0] == departure:
        tokens.pop(0)
    if tokens and tokens[-1] == arrival:
        tokens.pop()
    relevant = any(
        token not in {departure, arrival, "DCT", "DIRECT", "IFR", "VFR"}
        and not SPEED_LEVEL.fullmatch(token)
        for token in tokens
    )
    route = " ".join(tokens)
    return route if relevant and ROUTE_FORMAT.fullmatch(route) else None


class ForeFlightError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ForeFlightDraftCleanupError(ForeFlightError):
    pass


def _charter_flight(departure, arrival, departure_utc, aircraft, passengers):
    if departure_utc.utcoffset() is None:
        raise ValueError("ForeFlight departure time must include a timezone")
    return {
        "departure": departure,
        "destination": arrival,
        "scheduledTimeOfDeparture": departure_utc.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "aircraftRegistration": aircraft,
        "load": {"passengers": [{"type": "Adult"} for _ in range(passengers)]},
        "windOptions": {"windModel": "Forecasted"},
        "flightRule": "IFR",
        "atcTypeOfFlight": "N",
    }


def _has_route(result, departure, arrival):
    if not isinstance(result, dict):
        return False
    data = result.get("flightData") or {}
    route = data.get("routeToDestination") or {}
    return bool(enroute_text(route.get("route"), departure, arrival))


class ForeFlightClient:
    def __init__(self, config=settings, *, transport=None):
        self.config = config
        self._transport = transport
        self._http = None

    async def __aenter__(self):
        if self._http is not None:
            raise RuntimeError("ForeFlight client is already open")
        url = urlsplit(self.config.foreflight_api_url or BASE_URL)
        if (
            url.scheme != "https"
            or url.hostname
            not in {"public-api.foreflight.com", "dispatch.foreflight.com"}
            or url.username
            or url.password
            or url.port not in (None, 443)
            or url.path.rstrip("/") not in ("", "/public/api")
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "ForeFlight API URL must use an official Dispatch HTTPS endpoint"
            )
        if not self.config.foreflight_api_key or not self.config.foreflight_vendor_id:
            raise ForeFlightError(
                "FOREFLIGHT_API_KEY and FOREFLIGHT_VENDOR_ID are required"
            )
        api_key = self.config.foreflight_api_key.get_secret_value().strip()
        vendor = self.config.foreflight_vendor_id.get_secret_value().strip()
        try:
            UUID(vendor)
        except ValueError:
            raise ForeFlightError("FOREFLIGHT_VENDOR_ID must be a UUID") from None
        if not api_key or any(c in api_key for c in "\r\n"):
            raise ForeFlightError("Invalid ForeFlight API key configuration")
        self._http = httpx.AsyncClient(
            base_url=BASE_URL + "/",
            timeout=self.config.api_timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
            headers={
                "x-api-key": api_key,
                "x-vendorId": vendor,
                "Accept": "application/json",
                "User-Agent": "OpsBot/1.0",
            },
        )
        return self

    async def __aexit__(self, *args):
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _get(self, path, *, params=None):
        if self._http is None:
            raise RuntimeError("Use ForeFlightClient as an async context manager")
        parts = path.split("/")
        static = {"apiKeyInfo", "aircraft", "Flights/flights", "savedroutes"}
        allowed = path in static
        if len(parts) in (2, 3) and parts[0] == "Flights" and parts[1] != "flights":
            self._flight_id(parts[1])
            allowed = len(parts) == 2 or parts[2] in DOCUMENTS | {
                "performance",
                "overflight",
            }
        if not allowed:
            raise PermissionError("ForeFlight endpoint is not allowed")
        return await self._send(self._http.get, path, params=params)

    async def _send(self, send, path, *, parse_json=True, **options):
        try:
            response = await send(path, **options)
        except httpx.HTTPError:
            raise ForeFlightError("ForeFlight connection failed") from None
        if response.status_code == 204:
            return None
        if not 200 <= response.status_code < 300:
            raise ForeFlightError(
                f"ForeFlight returned HTTP {response.status_code}", response.status_code
            )
        if not parse_json:
            return None
        try:
            return response.json()
        except ValueError:
            raise ForeFlightError(
                "ForeFlight returned an unexpected response format"
            ) from None

    @staticmethod
    def _flight_id(value):
        try:
            text = str(value)
            UUID(text)
            if len(text) not in (32, 36) or any(
                c not in "0123456789abcdefABCDEF-" for c in text
            ):
                raise ValueError
            return text
        except ValueError:
            raise ValueError(
                "ForeFlight flightId must be a UUID returned by ForeFlight"
            ) from None

    async def api_key_info(self):
        return await self._get("apiKeyInfo")

    async def list_aircraft(self):
        return await self._get("aircraft")

    async def list_flights(
        self, start: datetime, end: datetime, *, search: str | None = None
    ):
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("ForeFlight flight dates must include a timezone")
        if not timedelta(0) < end - start <= timedelta(days=31):
            raise ValueError(
                "ForeFlight flight window must be positive and at most 31 days"
            )
        params = {
            "fromDate": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "toDate": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if search:
            params["search"] = search
        return await self._get("Flights/flights", params=params)

    async def get_flight(self, flight_id: str):
        return await self._get(f"Flights/{self._flight_id(flight_id)}")

    async def get_performance(self, flight_id: str):
        return await self._get(f"Flights/{self._flight_id(flight_id)}/performance")

    async def get_overflight(self, flight_id: str):
        return await self._get(f"Flights/{self._flight_id(flight_id)}/overflight")

    async def get_document(self, flight_id: str, kind: str):
        if kind not in DOCUMENTS:
            raise ValueError("Unknown ForeFlight document type")
        return await self._get(f"Flights/{self._flight_id(flight_id)}/{kind}")

    async def list_saved_routes(self):
        return await self._get("savedroutes")

    async def calculate_route_performance(
        self,
        departure,
        arrival,
        departure_utc,
        aircraft,
        passengers,
        route,
        *,
        flight_level=None,
    ):
        if self._http is None:
            raise RuntimeError("Use ForeFlightClient as an async context manager")
        route = enroute_text(route, departure, arrival)
        if route is None:
            raise ValueError("ForeFlight route must contain explicit enroute routing")
        route_data = {"route": route}
        if flight_level is not None:
            route_data["altitude"] = {"altitude": flight_level, "unit": "FL"}
        flight = _charter_flight(
            departure, arrival, departure_utc, aircraft, passengers
        )
        flight["routeToDestination"] = route_data
        return await self._send(
            self._http.post, "Flights/performance", json={"flight": flight}
        )

    async def calculate_autorouted_performance(
        self, departure, arrival, departure_utc, aircraft, passengers
    ):
        if self._http is None:
            raise RuntimeError("Use ForeFlightClient as an async context manager")
        flight = _charter_flight(
            departure, arrival, departure_utc, aircraft, passengers
        )
        reference = f"OPS-CHARTER-{uuid4().hex[:16]}"
        flight["dispatcherNotes"] = (
            f"Temporary OPS Charter route calculation {reference}"
        )
        flight["tags"] = [reference]
        creation = asyncio.create_task(
            self._send(self._http.post, "Flights", json={"flight": flight}, timeout=8)
        )
        cancelled = False
        try:
            try:
                response = await asyncio.shield(creation)
            except asyncio.CancelledError:
                # A disconnected Mini App may cancel while Dispatch is creating the draft.
                cancelled = True
                response = await creation
        except ForeFlightError as error:
            if error.status_code is None:
                await self._recover_charter_draft(reference, flight, departure_utc)
            raise
        created = response.get("flight") if isinstance(response, dict) else None
        if not isinstance(created, dict) or not created.get("flightId"):
            await self._recover_charter_draft(reference, flight, departure_utc)
            raise ForeFlightError("ForeFlight did not return the temporary flight ID")
        flight_id = self._flight_id(created["flightId"])
        try:
            if cancelled:
                raise asyncio.CancelledError
            if _has_route(created, departure, arrival):
                return created
            # Auto Route runs in Dispatch and can publish the calculated route asynchronously.
            for wait in (0, 0.5, 1.0):
                if wait:
                    await asyncio.sleep(wait)
                updated = await self.get_performance(flight_id)
                if _has_route(updated, departure, arrival):
                    return updated
            return None
        finally:
            await self._delete_charter_draft(flight_id)

    async def _recover_charter_draft(self, reference, flight, stamp):
        params = {
            "fromDate": (stamp - timedelta(days=1)).astimezone(UTC).isoformat(),
            "toDate": (stamp + timedelta(days=1)).astimezone(UTC).isoformat(),
            "tags": reference,
        }
        try:
            response = await self._send(
                self._http.get, "Flights/flights", params=params, timeout=5
            )
        except ForeFlightError as error:
            if error.status_code == 404:
                return
            logger.error(
                "Unable to reconcile temporary Charter flight tag {}", reference
            )
            raise ForeFlightDraftCleanupError(
                f"Check temporary flight {reference} in Dispatch."
            ) from None
        rows = response.get("flights", []) if isinstance(response, dict) else []
        for row in rows:
            if (
                isinstance(row, dict)
                and reference in (row.get("tags") or [])
                and all(
                    row.get(field) == flight[field]
                    for field in ("aircraftRegistration", "departure", "destination")
                )
            ):
                await self._delete_charter_draft(self._flight_id(row.get("flightId")))

    async def _delete_charter_draft(self, flight_id):
        try:
            await self._send(
                self._http.delete, f"Flights/{flight_id}", parse_json=False, timeout=5
            )
        except ForeFlightError as error:
            if error.status_code != 404:
                logger.error("Could not delete temporary Charter flight {}", flight_id)
                raise ForeFlightDraftCleanupError(
                    f"Delete temporary ForeFlight calculation {flight_id} in Dispatch."
                ) from None
