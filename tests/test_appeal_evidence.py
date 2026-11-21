"""Independent re-inspection evidence for lots that opted in.

Synthetic scale: a reading from the registered round may be reduced by 0, 1
or 2 to obtain the true value; G1 starts at 7, G2 at 5 and G3 at 3.  One lot,
two original samples, three thresholds and a single verification round per
day are enough to exercise every contract path.
"""

import json
import sys
from pathlib import Path

from marketproof import appeal_capture, grade, index, quote, trade
from marketproof.store import connect, read

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def grade_payload(opt_in=True):
    lot = {"id": "lot-1", "origin": "farm-1", "product": "tomato", "harvest_day": 8,
           "quantity": 100, "expires_day": 20}
    if opt_in:
        lot["independent_review"] = True
    return {
        "source_id": "grades-v1",
        "business_time": 10,
        "lots": [lot],
        "inspections": [
            {"id": "inspect-1", "lot_id": "lot-1", "origin": "farm-1", "product": "tomato",
             "harvest_day": 8, "grade": "G1", "sampled_at": 9, "version": 1},
        ],
    }


def enable_review_payload():
    return {
        "source_id": "enable-review",
        "business_time": 10,
        "lots": [{"id": "lot-1", "origin": "farm-1", "product": "tomato", "harvest_day": 8,
                  "quantity": 100, "expires_day": 20, "independent_review": True}],
        "inspections": [],
    }


def appeal_context():
    return {
        "source_id": "appeal-ctx-1",
        "business_time": 11,
        "inspection": {"id": "lab-letter-1", "lot_id": "lot-1", "origin": "farm-1",
                       "product": "tomato", "harvest_day": 8, "grade": "G2",
                       "sampled_at": 9, "version": 2},
        "independent_review": {
            "lot_id": "lot-1",
            "round": "round-A",
            "offsets": [0, 1, 2],
            "thresholds": {"G1_min": 7, "G2_min": 5, "G3_min": 3},
            "registered_range": [0, 10],
            "samples": [
                {"id": "sample-m", "specimen": "specimen-1", "round": "round-A", "reading": 9,
                 "preserved": True, "operator": "op-market", "witness": "wit-1", "claim": "G1"},
                {"id": "sample-l", "specimen": "specimen-2", "round": "round-A", "reading": 8,
                 "preserved": True, "operator": "op-lab", "witness": "wit-2", "claim": "G2"},
            ],
            "references": [
                {"id": "std-7", "kind": "standard", "true_value": 7, "round": "round-A", "available": True},
                {"id": "ref-7", "kind": "threshold", "threshold": 7, "available": True, "preserved": True},
                {"id": "ref-5", "kind": "threshold", "threshold": 5, "available": True, "preserved": True},
                {"id": "ref-3", "kind": "threshold", "threshold": 3, "available": True, "preserved": True},
            ],
        },
    }


def absolute_verification(source_id="appeal-verify-abs", vid="verify-abs-1", day=12,
                          round_="round-A", reading=8):
    return {
        "source_id": source_id,
        "business_time": day,
        "independent_review": {
            "lot_id": "lot-1",
            "verification": {
                "id": vid, "day": day, "procedure": "absolute_reference", "round": round_,
                "standard": "std-7", "standard_reading": reading, "witness": "wit-3",
            },
        },
    }


def paired_verification(source_id="appeal-verify-paired", vid="verify-paired-1", day=13,
                        comparisons=None):
    return {
        "source_id": source_id,
        "business_time": day,
        "independent_review": {
            "lot_id": "lot-1",
            "verification": {
                "id": vid, "day": day, "procedure": "paired_comparison", "round": "round-B",
                "sample": "specimen-2", "sample_reading": 8,
                "comparisons": comparisons if comparisons is not None else [
                    {"reference": "ref-7", "reading": 9},
                    {"reference": "ref-5", "reading": 7},
                ],
                "witness": "wit-4", "order_preserved": True,
            },
        },
    }


def seed(db, opt_in=True):
    grade.run(db, grade_payload(opt_in=opt_in))


def seed_appeal(db):
    return appeal_capture.run(db, appeal_context())


def decision(db):
    return tuple(db.execute("SELECT grade,status FROM grade_decision WHERE lot_id='lot-1'").fetchone())


def test_same_reading_supports_two_grades_and_delivers_pending_slip():
    db = connect(":memory:")
    try:
        seed(db)
        result = seed_appeal(db)
        assert result["status"] == "pending_evidence"
        assert result["compatible_grades"] == ["G1", "G2"]
        assert result["grade"] is None and result["lot_status"] == "disputed"
        # the letter is retained as an old-category opinion, not voted on
        assert db.execute(
            "SELECT status FROM inspection_result WHERE id='lab-letter-1'").fetchone()[0] == "valid"
        plans = {p["procedure"]: p for p in result["plan"]}
        assert plans["paired_comparison"]["guarantees_grade"] is True
        assert plans["absolute_reference"]["guarantees_grade"] is False
        inconsistent = [o for o in plans["absolute_reference"]["observations"]
                        if o["conclusion"].get("status") == "inconsistent"]
        assert [o["offset"] for o in inconsistent] == [2]
        assert result["needed"] == []
        assert result["verification_budget"] == {"day": 11, "rounds_per_day": 1, "used": 0}
    finally:
        db.close()


def test_shared_curve_explains_both_originals_with_one_offset():
    db = connect(":memory:")
    try:
        seed(db)
        result = seed_appeal(db)
        explanations = {e["offset"]: e["grades"] for e in result["explanations"]}
        assert explanations == {
            0: {"specimen-1": "G1", "specimen-2": "G1"},
            1: {"specimen-1": "G1", "specimen-2": "G1"},
            2: {"specimen-1": "G1", "specimen-2": "G2"},
        }
    finally:
        db.close()


def test_absolute_reference_on_same_round_signs_g1():
    db = connect(":memory:")
    try:
        seed(db)
        seed_appeal(db)
        result = appeal_capture.run(db, absolute_verification())
        assert result["status"] == "resolved"
        assert result["grade"] == "G1"
        assert result["evidence"] == ["verify-abs-1"]
        assert decision(db) == ("G1", "saleable")
        conclusion = json.loads(db.execute(
            "SELECT conclusion FROM review_verification WHERE verification_id='verify-abs-1'"
        ).fetchone()[0])
        assert conclusion["offset"] == 1
        assert conclusion["true_values"] == {"specimen-1": 8, "specimen-2": 7}
    finally:
        db.close()


def test_paired_comparison_signs_g2_without_absolute_offset():
    db = connect(":memory:")
    try:
        seed(db)
        seed_appeal(db)
        result = appeal_capture.run(db, paired_verification(day=12))
        assert result["status"] == "resolved"
        assert result["grade"] == "G2"
        conclusion = json.loads(db.execute(
            "SELECT conclusion FROM review_verification WHERE verification_id='verify-paired-1'"
        ).fetchone()[0])
        assert (conclusion["lower_bound"], conclusion["upper_bound"]) == (5, 7)
        assert "offset" not in conclusion and "true_values" not in conclusion
        assert decision(db) == ("G2", "saleable")
    finally:
        db.close()


def test_missing_lower_threshold_only_excludes_one_grade():
    db = connect(":memory:")
    try:
        seed(db)
        package = appeal_context()
        package["independent_review"]["references"] = [
            r for r in package["independent_review"]["references"] if r["id"] != "ref-5"
        ]
        appeal_capture.run(db, package)
        result = appeal_capture.run(
            db, paired_verification(day=12, comparisons=[{"reference": "ref-7", "reading": 9}]))
        assert result["status"] == "pending_evidence"
        assert result["partial"] == {"excluded": ["G1"], "remaining": ["G2", "G3"]}
        assert result["grade"] is None and result["lot_status"] == "disputed"
        assert result["needed"] == [{"kind": "threshold_reference", "threshold": 5}]
        stored = json.loads(db.execute(
            "SELECT conclusion FROM review_verification WHERE verification_id='verify-paired-1'"
        ).fetchone()[0])
        assert "offset" not in stored and "true_values" not in stored
    finally:
        db.close()


def test_missing_original_sample_and_references_yield_partial_delivery():
    db = connect(":memory:")
    try:
        seed(db)
        package = appeal_context()
        for sample in package["independent_review"]["samples"]:
            sample["preserved"] = False
        package["independent_review"]["references"] = []
        appeal_capture.run(db, package)
        result = appeal_capture.run(db, paired_verification(day=12))
        assert result["status"] == "rejected"
        assert result["reason"] == "sample_not_preserved"
        kinds = [n["kind"] for n in result["needed"]]
        assert "preserved_sample" in kinds
        assert "standard" in kinds
        assert kinds.count("threshold_reference") == 2
        assert decision(db) == (None, "disputed")
    finally:
        db.close()


def test_other_round_scale_cannot_sign_the_registered_round():
    db = connect(":memory:")
    try:
        seed(db)
        seed_appeal(db)
        wrong = absolute_verification(source_id="appeal-verify-wrong", vid="verify-wrong",
                                      round_="round-B")
        rejected = appeal_capture.run(db, wrong)
        assert rejected["status"] == "rejected"
        assert rejected["reason"] == "round_mismatch"
        assert db.execute("SELECT COUNT(*) FROM review_verification").fetchone()[0] == 0
        # a rejected intake does not consume the day's single round
        resolved = appeal_capture.run(db, absolute_verification())
        assert resolved["status"] == "resolved" and resolved["grade"] == "G1"
    finally:
        db.close()


def test_late_conflicting_genuine_evidence_keeps_dispute_and_common_evidence():
    db = connect(":memory:")
    try:
        seed(db)
        seed_appeal(db)
        first = appeal_capture.run(db, absolute_verification(day=12))
        assert first["status"] == "resolved" and first["grade"] == "G1"
        second = appeal_capture.run(db, paired_verification(day=13))
        assert second["status"] == "disputed"
        assert second["kept_evidence"] == ["verify-abs-1", "verify-paired-1"]
        assert decision(db) == (None, "disputed")
        # a later round agreeing with either side cannot pick a winner
        third = appeal_capture.run(
            db, absolute_verification(source_id="appeal-verify-abs-2", vid="verify-abs-2", day=14))
        assert third["status"] == "disputed"
        assert third["kept_evidence"] == ["verify-abs-1", "verify-paired-1"]
        assert db.execute("SELECT COUNT(*) FROM review_verification").fetchone()[0] == 3
    finally:
        db.close()


def test_replay_and_byte_conflict_are_retained_without_overwrite():
    db = connect(":memory:")
    try:
        seed(db)
        seed_appeal(db)
        appeal_capture.run(db, absolute_verification())
        assert appeal_capture.run(db, absolute_verification()) == {"status": "duplicate"}
        changed = absolute_verification()
        changed["independent_review"]["verification"]["standard_reading"] = 9
        assert appeal_capture.run(db, changed) == {"status": "conflict"}
        assert db.execute("SELECT COUNT(*) FROM review_verification").fetchone()[0] == 1
        payload = json.loads(db.execute(
            "SELECT payload FROM review_verification WHERE verification_id='verify-abs-1'"
        ).fetchone()[0])
        assert payload["standard_reading"] == 8
        assert decision(db) == ("G1", "saleable")
        assert db.execute("SELECT COUNT(*) FROM source_conflict").fetchone()[0] == 1
    finally:
        db.close()


def test_only_one_verification_round_per_day():
    db = connect(":memory:")
    try:
        seed(db)
        seed_appeal(db)
        appeal_capture.run(db, absolute_verification(day=12))
        result = appeal_capture.run(db, paired_verification(day=12))
        assert result["status"] == "rejected"
        assert result["reason"] == "verification_round_used"
        assert result["used_by"] == "verify-abs-1"
        assert db.execute("SELECT COUNT(*) FROM review_verification").fetchone()[0] == 1
    finally:
        db.close()


def test_lot_without_optin_keeps_sampling_time_dispute():
    db = connect(":memory:")
    try:
        seed(db, opt_in=False)
        result = appeal_capture.run(db, appeal_context())
        assert result == {"grade": None, "status": "disputed", "review": "not_enabled"}
        assert db.execute("SELECT COUNT(*) FROM review_context").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM review_decision").fetchone()[0] == 0
    finally:
        db.close()


def test_claim_outside_the_registered_scale_is_unsupported():
    db = connect(":memory:")
    try:
        seed(db)
        package = appeal_context()
        package["inspection"]["grade"] = "G3"
        result = appeal_capture.run(db, package)
        assert result["status"] == "pending_evidence"
        assert {"inspection": "lab-letter-1", "claim": "G3"} in result["unsupported_claims"]
        assert result["grade"] is None and result["lot_status"] == "disputed"
    finally:
        db.close()


def test_same_specimen_on_two_forms_is_not_a_replacement():
    db = connect(":memory:")
    try:
        seed(db)
        seed_appeal(db)
        again = {
            "source_id": "appeal-ctx-2", "business_time": 11,
            "independent_review": {"lot_id": "lot-1", "samples": [
                {"id": "sample-m-dup", "specimen": "specimen-1", "round": "round-A", "reading": 9,
                 "preserved": True, "operator": "op-market", "witness": "wit-1", "claim": "G1",
                 "form_layout": "lab-b"},
            ]},
        }
        appeal_capture.run(db, again)
        assert db.execute("SELECT COUNT(*) FROM review_sample WHERE lot_id='lot-1'").fetchone()[0] == 2
        assert db.execute(
            "SELECT COUNT(*) FROM decision WHERE system='sample-identity' AND status='conflict'"
        ).fetchone()[0] == 0
        changed = {
            "source_id": "appeal-ctx-3", "business_time": 11,
            "independent_review": {"lot_id": "lot-1", "samples": [
                {"id": "sample-m-dup2", "specimen": "specimen-1", "round": "round-A", "reading": 9,
                 "preserved": True, "operator": "op-market", "witness": "wit-9", "claim": "G1"},
            ]},
        }
        appeal_capture.run(db, changed)
        assert db.execute("SELECT COUNT(*) FROM review_sample WHERE lot_id='lot-1'").fetchone()[0] == 2
        assert db.execute(
            "SELECT COUNT(*) FROM decision WHERE system='sample-identity' AND status='conflict'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT witness FROM review_sample WHERE specimen='specimen-1'").fetchone()[0] == "wit-1"
    finally:
        db.close()


def test_evidence_resolution_reconciles_existing_quotes_and_trades():
    db = connect(":memory:")
    try:
        quote.run(db, read(FIXTURES / "quote_valid.json"))
        grade.run(db, read(FIXTURES / "grade_valid.json"))
        grade.run(db, enable_review_payload())
        trade.run(db, read(FIXTURES / "trade_valid.json"))
        index.run(db, read(FIXTURES / "index_valid.json"))
        assert db.execute(
            "SELECT status FROM trade_receipt WHERE id='trade-1'").fetchone()[0] == "confirmed"
        appeal_capture.run(db, appeal_context())
        result = appeal_capture.run(db, paired_verification(day=12))
        assert result["status"] == "resolved" and result["grade"] == "G2"
        assert db.execute(
            "SELECT status FROM trade_receipt WHERE id='trade-1'").fetchone()[0] == "grade_dispute"
        latest = db.execute("SELECT * FROM index_release ORDER BY id DESC LIMIT 1").fetchone()
        assert (latest["status"], latest["min_price"], latest["stall_count"]) == ("published", 150, 2)
    finally:
        db.close()


def test_cli_appeal_import_entry_runs_the_evidence_pipeline(tmp_path, monkeypatch, capsys):
    from marketproof import __main__
    assert __main__.COMMANDS["appeal-import"] is appeal_capture
    db_path = tmp_path / "cli.sqlite"
    db = connect(db_path)
    grade.run(db, grade_payload())
    db.commit()
    db.close()
    package = appeal_context()
    package["source_id"] = "appeal-cli"
    package["business_time"] = 12
    package["independent_review"]["verification"] = {
        "id": "verify-cli", "day": 12, "procedure": "absolute_reference", "round": "round-A",
        "standard": "std-7", "standard_reading": 8, "witness": "wit-3",
    }
    input_file = tmp_path / "appeal.json"
    input_file.write_text(json.dumps(package), encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv",
        ["marketproof", "appeal-import", "--db", str(db_path), "--input", str(input_file)])
    __main__.main()
    out = json.loads(capsys.readouterr().out)
    assert out["result"]["status"] == "resolved"
    assert out["result"]["grade"] == "G1"
