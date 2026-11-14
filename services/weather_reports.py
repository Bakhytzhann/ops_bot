"""Compact raw-weather presentation, with deduplicated source attribution."""

import re
from datetime import UTC, datetime, timedelta

from config.settings import settings
from services.snapshot_models import utc
from services.weather_decode import summarize_metar


def metar_age_warning(raw, metadata, now=None):
    now = now or datetime.now(UTC)
    match = re.search(r"\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)", metadata)
    observed = utc(match[0]) if match else None
    if observed is None:
        stamp = re.search(r"\b(\d\d)(\d\d)(\d\d)Z\b", raw)
        if stamp:
            candidates = []
            for reference in (now, now.replace(day=1) - timedelta(days=1)):
                try:
                    candidate = reference.replace(
                        day=int(stamp[1]),
                        hour=int(stamp[2]),
                        minute=int(stamp[3]),
                        second=0,
                        microsecond=0,
                    )
                    if candidate <= now + timedelta(minutes=10):
                        candidates.append(candidate)
                except ValueError:
                    continue
            observed = max(candidates) if candidates else None
    if observed is None:
        return "Свежесть METAR: время наблюдения не определено."
    if now - observed > timedelta(hours=settings.weather_stale_hours):
        return "METAR устарел: это последнее доступное наблюдение."
    if observed > now + timedelta(minutes=10):
        return "Время METAR в будущем; требуется проверка."
    return ""


def _time(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).isoformat()
    return str(value) if value is not None else "—"


def leon_reports(data, include_taf=False):
    reports = []
    for item in data:
        for suffix in ("Adep", "Ades"):
            airport = item.get("airport" + suffix) or {}
            station = (airport.get("code") or {}).get("icao") or suffix.upper()
            for product in ("metar", "taf") if include_taf else ("metar",):
                value = item.get(product + suffix)
                if isinstance(value, dict):
                    raw = value.get("rawText")
                    metadata = (
                        f"Наблюдение: {value.get('observationTime') or '—'}"
                        if product == "metar"
                        else f"Действует: {value.get('validFrom') or '—'} — {value.get('validTo') or '—'}"
                    )
                else:
                    raw, metadata = value, ""
                reports.append(
                    {
                        "source": "Leon MCP",
                        "station": station,
                        "product": product.upper(),
                        "raw": raw,
                        "metadata": metadata,
                    }
                )
    return reports


def awc_reports(result, include_taf=False):
    reports, errors = [], []
    for product in result["products"]:
        kind = product["product"].upper()
        if kind == "TAF" and not include_taf:
            continue
        if product.get("error"):
            errors.append(f"AWC {kind}: ошибка получения ({product['error']}).")
            continue
        for station in result["airports"]:
            matches = [r for r in product["data"] if r.get("icaoId") == station]
            if not matches:
                reports.append(
                    {
                        "source": "AWC",
                        "station": station,
                        "product": kind,
                        "raw": None,
                        "metadata": "",
                    }
                )
            for row in matches:
                raw = row.get("rawOb") if kind == "METAR" else row.get("rawTAF")
                metadata = (
                    f"Наблюдение UTC: {_time(row.get('obsTime'))}"
                    if kind == "METAR"
                    else f"Действует UTC: {_time(row.get('validTimeFrom'))} — {_time(row.get('validTimeTo'))}"
                )
                reports.append(
                    {
                        "source": "AWC",
                        "station": station,
                        "product": kind,
                        "raw": raw,
                        "metadata": metadata,
                    }
                )
    return reports, errors


def format_reports(reports, errors=()):
    grouped = {}
    for report in reports:
        raw = report["raw"]
        if not raw or str(raw).strip() in {"N/A", "NIL"}:
            raw = None
        key = (
            report["station"],
            report["product"],
            " ".join(str(raw).split()) if raw else None,
        )
        if key not in grouped:
            grouped[key] = {**report, "raw": raw, "sources": [], "metadata_items": []}
        item = grouped[key]
        if report["source"] not in item["sources"]:
            item["sources"].append(report["source"])
        if report["metadata"] and report["metadata"] not in item["metadata_items"]:
            item["metadata_items"].append(report["metadata"])
    lines = []
    for item in grouped.values():
        header = f"{item['station']} | {item['product']} | {', '.join(item['sources'])}"
        if item["raw"] is None:
            lines.append(header + ": сводка не предоставлена источником.")
            continue
        block = [header, *item["metadata_items"], str(item["raw"])]
        if item["product"] == "METAR":
            warning = metar_age_warning(
                str(item["raw"]), " ".join(item["metadata_items"])
            )
            if warning:
                block.append(warning)
            block.append("Кратко: " + summarize_metar(str(item["raw"])))
        lines.append("\n".join(block))
    return (
        "\n\n".join([*lines, *errors])
        or "Погодные сводки не предоставлены источниками."
    )


def format_leon_weather(data, include_taf=False):
    return format_reports(leon_reports(data, include_taf))


def format_awc_weather(result, include_taf=False):
    reports, errors = awc_reports(result, include_taf)
    return format_reports(reports, errors)
