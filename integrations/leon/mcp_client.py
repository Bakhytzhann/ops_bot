import asyncio
import time
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from config.settings import settings

OPERATOR = settings.leon_operator

MCP_URL = settings.leon_mcp_url or f"https://{OPERATOR}.mcpserver.leon.aero/mcp"

TOKEN_URL = f"https://{OPERATOR}.leon.aero/access_token/refresh/"

# Only approved read-only MCP tools.
ALLOWED_TOOLS = frozenset(
    {
        "search-flights",
        "get-flights-by-flight-nids",
        "get-airport-handling-agents",
        "get-report-wizard-flight-scope-report",
        "get-flights-crew",
        "get-flight-watch-by-flight-nids",
        "get-weather-for-flight-list",
        "get-crew-by-wildcard",
        "get-contact-profiles-by-nid-list",
        "get-duty-list",
        "get-crew-ftl-sheet",
        "get-crew-ftl-violations",
        "get-quote-requests-list",
        "get-quote-requests-by-nid-list",
        "get-fleet",
        "get-aircraft-documents",
        "get-aoc-list",
        "get-flight-list-ops-checklist-items",
        "get-flight-list-sales-checklist-items",
        "get-flight-list-flight-care-checklist-items",
    }
)


class LeonMCPClient:
    def __init__(self):
        self._access_token = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def get_access_token(self, force=False):
        if (
            not force
            and self._access_token
            and time.monotonic() < self._token_expires_at
        ):
            return self._access_token

        async with self._token_lock:
            if (
                not force
                and self._access_token
                and time.monotonic() < self._token_expires_at
            ):
                return self._access_token

            if not OPERATOR:
                raise RuntimeError("LEON_OPERATOR is not configured")

            if not settings.leon_api_key:
                raise RuntimeError("LEON_API_KEY is not configured")

            api_key = settings.leon_api_key.get_secret_value()

            async with httpx.AsyncClient(
                timeout=30,
                trust_env=False,
            ) as http:
                response = await http.post(
                    TOKEN_URL,
                    data={"refresh_token": api_key},
                )
                response.raise_for_status()

                token = response.text.strip()

            if not token:
                raise RuntimeError("Leon returned an empty access token")

            # Refresh before the documented 30-minute expiry.
            self._access_token = token
            self._token_expires_at = time.monotonic() + 25 * 60

            return token

    @asynccontextmanager
    async def _session(self):
        token = await self.get_access_token()

        try:
            async with (
                httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=60,
                    trust_env=False,
                ) as http_client,
                streamable_http_client(MCP_URL, http_client=http_client) as (
                    read,
                    write,
                ),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                yield session
        except MCPError as exc:
            raise RuntimeError("Leon MCP request failed") from exc
        except ExceptionGroup as exc:
            _, unexpected = exc.split((MCPError, httpx.HTTPError))
            if unexpected is not None:
                raise
            raise RuntimeError("Leon MCP request failed") from exc

    async def list_allowed_tools(self):
        """Return schemas of approved tools only."""
        async with self._session() as session:
            result = await session.list_tools()
        return [tool for tool in result.tools if tool.name in ALLOWED_TOOLS]

    async def call_tool(
        self,
        name: str,
        arguments: dict,
    ):
        """Execute an approved read-only MCP tool."""

        if name not in ALLOWED_TOOLS:
            raise PermissionError(f"MCP tool is not allowed: {name}")

        if not isinstance(arguments, dict):
            raise TypeError("Tool arguments must be a dictionary")

        async with self._session() as session:
            result = await session.call_tool(
                name,
                arguments,
            )

        if result.is_error:
            raise RuntimeError(f"Leon MCP tool failed: {name}")

        return result


leon_mcp = LeonMCPClient()
