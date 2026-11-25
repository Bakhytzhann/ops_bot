from services.briefing_notams import applicability
from services.snapshot_models import utc

FILTERS = {"A": "AFFECT", "R": "REVIEW", "C": "ACTIVE", "T": "ALL"}


def notam_sets(snapshot, station):
    block = snapshot.blocks.get("notams")
    details = block.details if block else {}
    records = details.get("notams", {}).get(station, [])
    flight = applicability(
        records,
        utc(snapshot.flight.get("departureUTC")),
        utc(snapshot.flight.get("arrivalUTC")),
    )
    active = applicability(records, block.fetched_at, block.fetched_at) if block else []
    groups = {key: [] for key in FILTERS}
    for index, ((record, affect), (_, now)) in enumerate(
        zip(flight, active, strict=True)
    ):
        entry = (index, record)
        groups["T"].append(entry)
        if affect is True:
            groups["A"].append(entry)
        if affect is None:
            groups["R"].append(entry)
        if now is True:
            groups["C"].append(entry)
    return groups, station in details.get("recognized", set())
