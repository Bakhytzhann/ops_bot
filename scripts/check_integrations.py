"""Read-only service diagnostics without credentials or operational data output."""

import argparse
import asyncio

import httpx

from config.settings import settings
from integrations.foreflight.api_client import ForeFlightClient
from integrations.leon.mcp_client import leon_mcp


async def check(service):
    try:
        if service == "leon":
            tools = await leon_mcp.list_allowed_tools()
            print(f"Leon: connected, {len(tools)} allowed read-only tools")
        elif service == "foreflight":
            async with ForeFlightClient() as client:
                await client.api_key_info()
                aircraft = await client.list_aircraft()
            print(f"ForeFlight: connected, {len(aircraft)} aircraft")
        else:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                response = await client.get(
                    f"{settings.ollama_host.rstrip('/')}/api/tags"
                )
                response.raise_for_status()
                models = response.json().get("models", [])
            available = any(m.get("name") == settings.ollama_model for m in models)
            print(f"Ollama: connected, configured model available={available}")
            return available
        return True
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: print only sanitized error metadata
        status = getattr(exc, "status_code", None)
        print(
            f"{service}: failed ({type(exc).__name__})"
            + (f", HTTP {status}" if status else "")
        )
        return False


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "service",
        choices=("all", "leon", "foreflight", "ollama"),
        nargs="?",
        default="all",
    )
    args = parser.parse_args()
    services = (
        ("leon", "foreflight", "ollama") if args.service == "all" else (args.service,)
    )
    results = await asyncio.gather(*(check(service) for service in services))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
