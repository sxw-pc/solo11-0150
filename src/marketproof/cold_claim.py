"""First-pass contractual excursion threshold for journeys."""

import json

from .store import decide


def update_claim(db, journey):
    rows = db.execute(
        "SELECT payload FROM source WHERE system='cold' ORDER BY business_time, id"
    )
    records = [json.loads(row["payload"]) for row in rows]
    relevant = [row for row in records if row["journey"] == journey]
    latest = relevant[-1]
    duration = latest.get("alarm_minutes")
    if duration is None:
        status = "missing_summary"
    elif duration >= latest["trigger_minutes"]:
        status = "alarm_unattributed"
    else:
        status = "clear"
    decide(db, "cold_claim", journey, status, "aggregate alarm summary", [latest["source_id"]])
    return {"journey": journey, "status": status}
