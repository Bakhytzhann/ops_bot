from unittest.mock import AsyncMock

import httpx
import pytest

from integrations.leon import mcp_client


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
