import json
from unittest.mock import AsyncMock

import httpx
import pytest

from integrations.leon import mcp_client, tools


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_error", [False, True])
async def test_leon_normalizes_transport_error(monkeypatch, connection_error):
    def reply(request):
        if connection_error:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(403)

    http = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    monkeypatch.setattr(mcp_client, "MCP_URL", "https://example.com/mcp")
    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", lambda **_: http)
    client = mcp_client.LeonMCPClient()
    monkeypatch.setattr(client, "get_access_token", AsyncMock(return_value="test"))

    with pytest.raises(RuntimeError, match="Leon MCP request failed"):
        await client.call_tool("get-flights-by-flight-nids", {"flightNids": [1]})


@pytest.mark.asyncio
async def test_schedule_loads_when_tool_listing_rejects_token(monkeypatch):
    calls = []

    def reply(request):
        payload = json.loads(request.content)
        method = payload["method"]
        calls.append(method)
        if method == "notifications/initialized":
            return httpx.Response(202)
        envelope = {"jsonrpc": "2.0", "id": payload["id"]}
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "Leon", "version": "1"},
            }
        elif method == "tools/call":
            assert payload["params"]["name"] == "search-flights"
            result = {
                "content": [],
                "structuredContent": {"flightList": [{"flightNid": 121}]},
            }
        elif method == "tools/list":
            return httpx.Response(
                200,
                json={
                    **envelope,
                    "error": {
                        "code": -32001,
                        "message": "Invalid or expired access token",
                    },
                },
            )
        else:
            pytest.fail(f"Unexpected MCP method: {method}")
        return httpx.Response(200, json={**envelope, "result": result})

    http = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    client = mcp_client.LeonMCPClient()
    monkeypatch.setattr(mcp_client, "MCP_URL", "https://example.com/mcp")
    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", lambda **_: http)
    monkeypatch.setattr(client, "get_access_token", AsyncMock(return_value="test"))
    monkeypatch.setattr(tools, "leon_mcp", client)

    schedule = await tools.get_flight_schedule("2026-11-22")

    assert schedule["complete"]
    assert [flight["flightNid"] for flight in schedule["flights"]] == [121]
    assert calls == ["initialize", "notifications/initialized", "tools/call"]
