import asyncio
import re
from datetime import UTC, datetime

import httpx

BASE_URL = "https://aviationweather.gov/api/data"
SOURCE = "NOAA Aviation Weather Center (AWC)"


async def get_weather(airports: list[str], *, include_taf: bool = True) -> dict:
    airports = sorted({code.upper() for code in airports})
    if (
        not airports
        or len(airports) > 4
        or any(not re.fullmatch(r"[A-Z]{4}", c) for c in airports)
    ):
        raise ValueError("Укажите от одного до четырёх ICAO-кодов аэропортов.")
    async with httpx.AsyncClient(
        timeout=20,
        trust_env=False,
        headers={"User-Agent": "OpsBot/1.0 (weather reader)"},
    ) as client:

        async def fetch(product):
            url = f"{BASE_URL}/{product}"
            response = await client.get(
                url, params={"ids": ",".join(airports), "format": "json"}
            )
            if response.status_code == 204:
                return {
                    "product": product,
                    "data": [],
                    "url": str(response.request.url),
                }
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list) or not all(
                isinstance(row, dict) for row in data
            ):
                raise TypeError("Unexpected AWC response")
            return {"product": product, "data": data, "url": str(response.request.url)}

        products = ("metar", "taf") if include_taf else ("metar",)
        results = await asyncio.gather(
            *(fetch(p) for p in products), return_exceptions=True
        )
    return {
        "source": SOURCE,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "airports": airports,
        "products": [
            {"product": product, "error": type(result).__name__}
            if isinstance(result, Exception)
            else result
            for product, result in zip(products, results, strict=True)
        ],
    }
