from pathlib import Path

from marketproof import appeal_capture, grade
from marketproof.store import connect, read

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_appeal_changes_existing_inspection_grade_to_disputed():
    db = connect(":memory:")
    try:
        grade.run(db, read(FIXTURES / "grade_valid.json"))
        assert tuple(db.execute(
            "SELECT grade,status FROM grade_decision WHERE lot_id='lot-1'"
        ).fetchone()) == ("G1", "saleable")
        got = appeal_capture.run(db, read(FIXTURES / "grade-appeal.json"))
        assert got == {"grade": None, "status": "disputed"}
    finally:
        db.close()
