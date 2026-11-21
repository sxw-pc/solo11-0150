"""Bring a small decoded price slip into the existing quote ledger."""

from . import quote
from .store import ingest


def run(db, data):
    status = ingest(db, "price_slip", data)
    if status != "new":
        return {"status": status}
    raw = data["quote"]
    price = int(raw["displayed_price"])
    item = {"price": price, **raw["entry"]}
    return quote.run(db, {"source_id": data["source_id"] + "-quote", "quotes": [item]})
