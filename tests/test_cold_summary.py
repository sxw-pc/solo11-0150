from pathlib import Path

from marketproof import cold_intake
from marketproof.store import connect, read

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_cold_summary_changes_current_incident():
    with connect(":memory:") as db:
        first = cold_intake.run(db, read(FIXTURES / "cold_first.json"))
        arrived = cold_intake.run(db, read(FIXTURES / "cold_arrival.json"))
        assert first["status"] == "clear"
        assert arrived["status"] == "alarm_unattributed"
        history = db.execute(
            "SELECT status FROM decision WHERE system='cold_claim' ORDER BY id"
        ).fetchall()
        assert [row["status"] for row in history] == ["clear", "alarm_unattributed"]
