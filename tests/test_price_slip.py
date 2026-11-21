from pathlib import Path

from marketproof import price_slip, quote
from marketproof.store import connect, read

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_capture_licence_changes_quote_status_and_uses_displayed_integer():
    for status, expected in (("active", "valid"), ("suspended", "unauthorized")):
        with connect(":memory:") as db:
            original = read(FIXTURES / "quote_valid.json")
            original["licences"][0]["status"] = status
            quote.run(db, original)
            assert price_slip.run(db, read(FIXTURES / "price_slip_uncertain.json")) == {
                "quotes": [{"quote": "q-slip", "status": expected}]
            }
            saved = db.execute("SELECT price FROM quote_revision WHERE id='q-slip'").fetchone()
            assert saved["price"] == 12
