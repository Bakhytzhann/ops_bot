import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger
from pydantic import BaseModel


@dataclass
class Tool:
    name: str
    description: str
    parameters: type[BaseModel]
    handler: Callable[..., Any]
    timeout: float = 30.0


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")

        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def get_ollama_tools(self, names: set[str] | None = None) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters.model_json_schema(),
                },
            }
            for tool in self._tools.values()
            if names is None or tool.name in names
        ]

    async def execute(
        self,
        name: str,
        arguments: dict,
    ) -> dict:
        tool = self.get(name)
        logger.info("Tool call: {}", name)

        if tool is None:
            return {
                "ok": False,
                "error": f"Unknown tool: {name}",
            }

        try:
            validated = tool.parameters.model_validate(arguments)
            kwargs = validated.model_dump()

            if inspect.iscoroutinefunction(tool.handler):
                result = await asyncio.wait_for(
                    tool.handler(**kwargs),
                    timeout=tool.timeout,
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        tool.handler,
                        **kwargs,
                    ),
                    timeout=tool.timeout,
                )

            return {
                "ok": True,
                "data": result,
            }

        except Exception as exc:  # noqa: BLE001 -- isolate arbitrary tool failures; return only the error type
            return {
                "ok": False,
                "error": type(exc).__name__,
            }


registry = ToolRegistry()
