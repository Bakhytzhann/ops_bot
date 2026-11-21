from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from config.settings import settings

BASE_URL = "https://public-api.foreflight.com/public/api"
DOCUMENTS = frozenset({"briefing", "navlog", "icao", "overflightreport", "wb", "rwa"})


class ForeFlightError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


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
        try:
            response = await self._http.get(path, params=params)
        except httpx.HTTPError:
            raise ForeFlightError("ForeFlight connection failed") from None
        if response.status_code == 204:
            return None
        if not 200 <= response.status_code < 300:
            raise ForeFlightError(
                f"ForeFlight returned HTTP {response.status_code}", response.status_code
            )
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
                "ForeFlight flightId must be a UUID obtained from List Flights"
            ) from None

    async def api_key_info(self):
        return await self._get("apiKeyInfo")

    async def list_aircraft(self):
        return await self._get("aircraft")

    async def list_flights(self, start: datetime, end: datetime):
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("ForeFlight flight dates must include a timezone")
        if not timedelta(0) < end - start <= timedelta(days=31):
            raise ValueError(
                "ForeFlight flight window must be positive and at most 31 days"
            )
        return await self._get(
            "Flights/flights",
            params={
                "fromDate": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "toDate": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )

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
