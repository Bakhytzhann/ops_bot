from dataclasses import dataclass, field
from datetime import UTC, datetime


def utc(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(UTC) if stamp.tzinfo else None
    except ValueError:
        return None


def zulu(value):
    stamp = utc(value) if isinstance(value, str) else value
    return stamp.astimezone(UTC).strftime("%d %b %Y %H:%MZ") if stamp else "нет данных"


@dataclass
class SourceBlock:
    source: str
    text: str = ""
    status: str = "unavailable"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    details: dict = field(default_factory=dict)


@dataclass
class FlightSnapshot:
    flight: dict
    blocks: dict[str, SourceBlock] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Notam:
    identifier: str
    station: str
    kind: str
    replaces: str | None
    raw: str
    starts: datetime | None
    ends: datetime | None
    permanent: bool = False
    estimated_end: bool = False
    schedule: str = ""
    topic: str = ""

    def overlaps(self, start, end):
        if self.kind == "C":
            return False
        if not start or not end or end < start or not self.starts:
            return None
        if self.ends and (self.ends <= start or self.starts > end):
            return False
        if self.starts > end:
            return False
        if (
            self.schedule
            or self.estimated_end
            or (not self.ends and not self.permanent)
        ):
            return None
        return True

    def active_at(self, now):
        return self.overlaps(now, now)
