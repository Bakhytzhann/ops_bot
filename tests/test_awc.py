import httpx
import pytest

from integrations.awc import client as awc


@pytest.mark.asyncio
async def test_partial_awc_response(monkeypatch):
    real_client = httpx.AsyncClient

    def respond(request):
        assert request.url.params["ids"] == "EGLL,UTTT"
        assert request.headers["User-Agent"].startswith("OpsBot")
        if request.url.path.endswith("metar"):
            return httpx.Response(200, json=[{"icaoId": "EGLL", "rawOb": "EGLL RAW"}])
        return httpx.Response(503)

    monkeypatch.setattr(
        awc.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = await awc.get_weather(["UTTT", "EGLL", "UTTT"])
    assert result["products"][0]["data"][0]["rawOb"] == "EGLL RAW"
    assert result["products"][1]["error"] == "HTTPStatusError"


@pytest.mark.asyncio
async def test_awc_empty_204(monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        awc.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda req: httpx.Response(204)), **kwargs
        ),
    )
    result = await awc.get_weather(["UTTT"])
    assert all(p["data"] == [] for p in result["products"])


@pytest.mark.asyncio
async def test_compact_weather_does_not_request_taf(monkeypatch):
    real_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(204)

    monkeypatch.setattr(
        awc.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = await awc.get_weather(["UTTT"], include_taf=False)
    assert requests == ["/api/data/metar"]
    assert len(result["products"]) == 1


@pytest.mark.parametrize(
    "airports", [[], ["UTT"], ["../x"], ["UTTT"] * 5 + ["UAAA", "EGLL", "KJFK", "EDDF"]]
)
@pytest.mark.asyncio
async def test_awc_bad_station_rejected(airports):
    with pytest.raises(ValueError):
        await awc.get_weather(airports)
