"""Offline intake for synthetic cold transport logs."""

from .store import ingest
from .cold_claim import update_claim


def run(db, data):
    result = ingest(db, "cold", data)
    if result != "new":
        return {"status": result}
    return update_claim(db, data["journey"])
