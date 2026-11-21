"""Collect synthetic grading appeal slips for the current inspection ledger."""

from . import grade
from .store import ingest


def run(db, data):
    status = ingest(db, "appeal", data)
    if status != "new":
        return {"status": status}
    inspection = data["inspection"]
    grade.run(
        db,
        {
            "source_id": data["source_id"] + "-grade",
            "business_time": data["business_time"],
            "inspections": [inspection],
        },
    )
    entry = db.execute(
        "SELECT state.grade, state.status FROM grade_decision AS state WHERE lot_id=?",
        (inspection["lot_id"],),
    ).fetchone()
    return dict(entry)
