from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from agent.tool_registry import Tool, registry


class TimeInput(BaseModel):
    timezone: str = Field(
        default="Asia/Almaty",
        description="IANA timezone, e.g. Asia/Almaty or UTC",
    )


def get_current_time(timezone: str = "Asia/Almaty") -> dict:
    try:
        now = datetime.now(ZoneInfo(timezone))
    except (KeyError, ValueError):
        return {"error": "Invalid timezone"}

    return {
        "datetime": now.isoformat(),
        "timezone": timezone,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
    }


def register_system_tools():
    registry.register(
        Tool(
            name="get_current_time",
            description=(
                "Get the current date and time "
                "in a specified timezone. "
                "Use this tool when the user asks "
                "for the current date or time."
            ),
            parameters=TimeInput,
            handler=get_current_time,
        )
    )
