import re
from datetime import UTC, datetime

from services.snapshot_models import Notam

HEADER = re.compile(
    r"(?m)^([A-Z]\d{4}/\d{2})\s+NOTAM([NRC])(?:\s+([A-Z]\d{4}/\d{2}))?[^\n]*"
)
SECTION = re.compile(
    r"(?mi)^(Departure[^\n]*?|Destination[^\n]*?|Alternate[^\n]*?|Enroute[^\n]*?)\b([A-Z]{4})\s+-[^\n]*$"
)


def notam_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%y%m%d%H%M").replace(tzinfo=UTC)
    except ValueError:
        return None


def extract_notams(text, stations):
    text = re.sub(r"(?m)^NOTAMs\s+\d+\s+of\s+\d+\s*$", "", text)
    marker = re.search(r"(?mi)^NOTAMS\s*$", text)
    if not marker:
        raise ValueError("NOTAM section not recognized")
    text = text[marker.end() :]
    sections = list(SECTION.finditer(text))
    result = {station: [] for station in stations}
    recognized = set()
    for i, section in enumerate(sections):
        station = section[2].upper()
        if station not in result or not section[1].lower().startswith(
            ("departure", "destination")
        ):
            continue
        recognized.add(station)
        body = text[
            section.end() : sections[i + 1].start()
            if i + 1 < len(sections)
            else len(text)
        ]
        headers = list(HEADER.finditer(body))
        for j, header in enumerate(headers):
            end = headers[j + 1].start() if j + 1 < len(headers) else len(body)
            raw = body[header.start() : end].strip()
            if j + 1 < len(headers):
                lines = raw.splitlines()
                if lines and lines[-1].startswith("["):
                    lines.pop()
                if lines:
                    lines.pop()
                raw = "\n".join(lines)
            fields = {
                m[1]: m[2].strip()
                for m in re.finditer(r"([A-GQ])\)\s*(.*?)(?=\s+[A-GQ]\)|\Z)", raw, re.S)
            }
            if not re.search(rf"\b{re.escape(station)}\b", fields.get("A", "")):
                continue
            before = body[: header.start()].strip().splitlines()
            topic = next(
                (line for line in reversed(before[-2:]) if not line.startswith("[")), ""
            )
            end_value = fields.get("C", "")
            result[station].append(
                Notam(
                    identifier=header[1],
                    station=station,
                    kind=header[2],
                    replaces=header[3],
                    raw=raw,
                    starts=notam_time(fields.get("B")),
                    ends=notam_time(end_value[:10]),
                    permanent=end_value == "PERM",
                    estimated_end="EST" in end_value,
                    schedule=fields.get("D", ""),
                    topic=topic,
                )
            )
    if not recognized:
        raise ValueError("Airport NOTAM sections not recognized")
    return result, recognized


def applicability(records, start, end):
    replaced = {
        n.replaces
        for n in records
        if n.replaces and n.starts and start and n.starts <= start
    }
    return [
        (n, False if n.identifier in replaced else n.overlaps(start, end))
        for n in records
    ]


def counts(records, start, end):
    values = [value for _, value in applicability(records, start, end)]
    return (
        f"{sum(v is True for v in values)} · уточнить {sum(v is None for v in values)}"
    )
