"""Attribution window certification for cold-chain recorder originals.

Covers: the shared-offset acceptance geometry, dual clock-anchor and seal
work orders, contradictory witnesses without source priority, reset-visa
generations, one-budget intake advice, duplicate imports and the unchanged
market ledgers.
"""

import sys
from pathlib import Path

import pytest

from marketproof import cold_intake
from marketproof.cold_claim import attribute, collect_evidence
from marketproof.store import connect, read

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load(name):
    return read(FIXTURES / name)


@pytest.fixture()
def db():
    with connect(":memory:") as database:
        yield database


def seed_recorders(database):
    first = cold_intake.run(database, load("cold_first.json"))
    arrived = cold_intake.run(database, load("cold_arrival.json"))
    return first, arrived


def history(database, object_id):
    rows = database.execute(
        "SELECT status,reason FROM decision WHERE system='cold_attribution' "
        "AND object_id=? ORDER BY id",
        (object_id,),
    ).fetchall()
    return [(row["status"], row["reason"]) for row in rows]


def verdict_for(result, segment):
    return next(v for v in result["verdicts"] if v["segment"] == segment)


# ---------------------------------------------------------------------------
# Recorder geometry: one unknown common offset over the whole run
# ---------------------------------------------------------------------------

def test_acceptance_offsets_pin_first_second_or_neither(db):
    first, arrived = seed_recorders(db)
    assert first["status"] == "clear"
    assert arrived["status"] == "alarm_unattributed"

    result = attribute(db, "cool-J1")
    assert result["status"] == "alarm_unattributed"
    verdict = verdict_for(result, "seg-1")
    assert verdict["legal_offsets"] == [-1, 0, 1]
    by_offset = {v["offset"]: v for v in verdict["interpretations"].values()}
    # offset -1: zone [3,7), shipper gets exactly the 3-minute threshold.
    assert (by_offset[-1]["first_minutes"], by_offset[-1]["second_minutes"]) == (3, 1)
    # offset +1: zone [5,9), receiver gets exactly the threshold.
    assert (by_offset[1]["first_minutes"], by_offset[1]["second_minutes"]) == (1, 3)
    # offset 0: neither party reaches 3 continuous minutes.
    assert (by_offset[0]["first_minutes"], by_offset[0]["second_minutes"]) == (2, 2)


def test_separated_hot_zones_are_never_summed(db):
    seed_recorders(db)
    payload = load("cold_arrival.json")
    payload["source_id"] = "arrival-recorder-split"
    # One continuous 2-minute first-party zone; even another 2-minute zone
    # elsewhere could never be summed with it, and 2 < 3 anyway.
    payload["recorder"]["hot_on_local_clock"] = [4, 6]
    cold_intake.run(db, payload)
    result = attribute(db, "cool-J1")
    verdict = verdict_for(result, "seg-1")
    assert all(
        v["first_minutes"] < 3 and v["second_minutes"] < 3
        for v in verdict["interpretations"].values()
    )
    assert result["status"] == "alarm_unattributed"


# ---------------------------------------------------------------------------
# Work order 1: dual clock anchors over the whole preserved run
# ---------------------------------------------------------------------------

def test_dual_anchors_minus_one_certify_first_party(db):
    seed_recorders(db)
    result = cold_intake.run_anchor(db, load("cold_anchor_minus_one.json"))
    assert result["status"] == "attributed"
    verdict = verdict_for(result, "seg-1")
    assert verdict["party"] == "shipper"
    assert list(verdict["interpretations"]) == [-1]
    # The certificate uses one common calibration across the whole zone.
    assert result["pinned_offsets"] == [-1]
    assert history(db, "cool-J1")[-1][0] == "attributed"


def test_anchors_must_agree_on_one_common_offset(db):
    seed_recorders(db)
    bad = load("cold_anchor_minus_one.json")
    bad["source_id"] = "dual-anchor-bad"
    bad["anchors"] = [
        {"local_time": 2, "unified_time": 1},   # implies -1
        {"local_time": 10, "unified_time": 11},  # implies +1
    ]
    result = cold_intake.run_anchor(db, bad)
    assert result["status"] == "evidence_conflict"
    assert history(db, "cool-J1")[-1][0] == "evidence_conflict"


def test_anchor_plus_one_certifies_second_party(db):
    seed_recorders(db)
    plus_one = load("cold_anchor_minus_one.json")
    plus_one["source_id"] = "dual-anchor-run-plus"
    plus_one["anchors"] = [
        {"local_time": 2, "unified_time": 3},
        {"local_time": 10, "unified_time": 11},
    ]
    result = cold_intake.run_anchor(db, plus_one)
    verdict = verdict_for(result, "seg-1")
    assert result["status"] == "attributed"
    assert verdict["party"] == "receiver"


# ---------------------------------------------------------------------------
# Work order 2: independent temperature-sensitive seals
# ---------------------------------------------------------------------------

def test_rear_seal_certifies_without_pinning_recorder_offset(db):
    seed_recorders(db)
    result = cold_intake.run_seal(db, load("cold_seal_rear.json"))
    assert result["status"] == "attributed"
    verdict = verdict_for(result, "seg-1")
    assert verdict["party"] == "receiver"
    # Seal path must not fill ±1 into the archive.
    assert "interpretations" not in verdict
    assert "offset" not in verdict
    # Recorder alarm total is preserved unchanged.
    recorders = collect_evidence(db, "cool-J1")["recorders"]
    assert recorders[-1]["alarm_minutes"] == 4
    rows = db.execute(
        "SELECT party,alarm_minutes,range_start,range_end FROM cold_seal ORDER BY id"
    ).fetchall()
    assert {row["party"]: row["alarm_minutes"] for row in rows} == {
        "shipper": 2,
        "receiver": 3,
    }


def test_seal_alone_without_counterparty_proof_does_not_certify(db):
    seed_recorders(db)
    only_rear = load("cold_seal_rear.json")
    only_rear["source_id"] = "rear-seal-only"
    only_rear["seals"] = [only_rear["seals"][0]]
    result = cold_intake.run_seal(db, only_rear)
    assert result["status"] == "alarm_unattributed"
    assert verdict_for(result, "seg-1")["party"] is None


def test_seal_identity_or_goods_mismatch_is_rejected(db):
    seed_recorders(db)
    bad = load("cold_seal_rear.json")
    bad["source_id"] = "rear-seal-mismatch"
    bad["seals"][0]["identity_ok"] = False
    result = cold_intake.run_seal(db, bad)
    assert result["status"] == "alarm_unattributed"


def test_contradictory_witnesses_stop_at_evidence_conflict(db):
    seed_recorders(db)
    cold_intake.run_anchor(db, load("cold_anchor_minus_one.json"))  # shipper
    conflict = load("cold_seal_rear.json")
    conflict["source_id"] = "rear-seal-conflict"
    # Rear seal [6,9) is outside the pinned recorder zone [3,7) and names
    # the other party; no source priority may pick a winner.
    result = cold_intake.run_seal(db, conflict)
    assert result["status"] == "evidence_conflict"
    verdict = verdict_for(result, "seg-1")
    assert verdict["party"] is None
    assert "conflict" in verdict["reason"]
    assert history(db, "cool-J1")[-1][0] == "evidence_conflict"


def test_anchor_and_seal_same_party_jointly_certify(db):
    seed_recorders(db)
    cold_intake.run_anchor(db, load("cold_anchor_minus_one.json"))
    joint = load("cold_seal_rear.json")
    joint["source_id"] = "front-seal-joint"
    joint["seals"] = [
        {
            "seal_id": "seal-front-joint",
            "party": "shipper",
            "alarm_range": [3, 6],
            "identity_ok": True,
            "goods_ok": True,
        },
        {
            "seal_id": "seal-rear-low",
            "party": "receiver",
            "alarm_range": [7, 8],
            "identity_ok": True,
            "goods_ok": True,
        },
    ]
    result = cold_intake.run_seal(db, joint)
    verdict = verdict_for(result, "seg-1")
    assert result["status"] == "attributed"
    assert verdict["party"] == "shipper"


# ---------------------------------------------------------------------------
# Reset visas: breakpoints must not cross generations
# ---------------------------------------------------------------------------

def test_reset_visa_splits_generations_and_keeps_old_certificate(db):
    seed_recorders(db)
    cold_intake.run_anchor(db, load("cold_anchor_minus_one.json"))
    assert history(db, "cool-J1")[-1][0] == "attributed"

    # A second alarm arrives for a later device run segment; the earlier
    # calibrated window cannot serve as its credential.  The zone straddles
    # the handover so no party qualifies while the offset stays unknown.
    second = load("cold_arrival.json")
    second["source_id"] = "arrival-recorder-gen2"
    second["segment"] = "seg-2"
    second["recorder"]["hot_on_local_clock"] = [4, 8]
    second["alarm_minutes"] = 4
    cold_intake.run(db, second)

    reset = cold_intake.run_reset(db, load("cold_reset.json"))
    segments = {v["segment"]: v for v in reset["verdicts"]}
    assert "seg-1" in segments and "seg-2" in segments
    # The old whole-run object is superseded, its attributed row retained.
    statuses = [row[0] for row in history(db, "cool-J1")]
    assert statuses[-1] == "superseded" and "attributed" in statuses
    # No cross-generation offset: seg-2 still has all three legal offsets.
    assert segments["seg-2"]["legal_offsets"] == [-1, 0, 1]
    assert segments["seg-2"]["status"] == "alarm_unattributed"

    # A segment-scoped anchor can now certify only its own generation.
    gen2_anchor = load("cold_anchor_minus_one.json")
    gen2_anchor["source_id"] = "dual-anchor-gen2"
    gen2_anchor["segment"] = "seg-2"
    gen2_anchor["anchors"] = [
        {"local_time": 12, "unified_time": 13},
        {"local_time": 20, "unified_time": 21},
    ]
    result = cold_intake.run_anchor(db, gen2_anchor)
    seg2 = verdict_for(result, "seg-2")
    assert seg2["party"] == "receiver"
    seg1 = verdict_for(result, "seg-1")
    assert seg1["party"] == "shipper"


def test_whole_run_anchor_cannot_calibrate_later_generation(db):
    seed_recorders(db)
    second = load("cold_arrival.json")
    second["source_id"] = "arrival-recorder-gen2"
    second["segment"] = "seg-2"
    cold_intake.run(db, second)
    cold_intake.run_reset(db, load("cold_reset.json"))

    whole = load("cold_anchor_minus_one.json")
    whole["source_id"] = "dual-anchor-whole"
    whole.pop("segment")  # whole-run anchor after the reset
    result = cold_intake.run_anchor(db, whole)
    seg2 = verdict_for(result, "seg-2")
    assert seg2["legal_offsets"] == [-1, 0, 1]
    assert seg2["status"] == "alarm_unattributed"


# ---------------------------------------------------------------------------
# One-budget advice: coverage of the interpretation groups, not recency
# ---------------------------------------------------------------------------

def test_recommend_anchor_when_offset_ambiguity_dominates(db):
    seed_recorders(db)
    # The seal set is the most recently observed original, but only anchors
    # can separate the surviving offset interpretations.
    obtainable = {"anchor_region": True, "seal_range": True}
    result = cold_intake.run_recommend(
        db, {"journey": "cool-J1", "obtainable": obtainable}
    )
    assert result["recommendation"] == "cold-anchor-import"


def test_recommend_seal_when_only_same_range_seals_discriminate(db):
    seed_recorders(db)
    # A symmetric 3-minute zone around the handover: under every legal
    # offset each party stays below the 3-minute threshold, so no clock
    # calibration can ever draw a liability position; same-range seals can.
    payload = load("cold_arrival.json")
    payload["source_id"] = "arrival-recorder-symmetric"
    payload["recorder"]["hot_on_local_clock"] = [5, 8]
    payload["recorder"]["clock_offset_bounds"] = [0, 0]
    cold_intake.run(db, payload)
    result = cold_intake.run_recommend(
        db,
        {"journey": "cool-J1", "obtainable": {"anchor_region": True, "seal_range": True}},
    )
    assert result["recommendation"] == "cold-seal-import"


def test_recommend_respects_what_the_site_can_actually_obtain(db):
    seed_recorders(db)
    result = cold_intake.run_recommend(
        db, {"journey": "cool-J1", "obtainable": {"seal_range": True}}
    )
    assert result["recommendation"] == "cold-seal-import"
    result = cold_intake.run_recommend(
        db, {"journey": "cool-J1", "obtainable": {"anchor_region": True}}
    )
    assert result["recommendation"] == "cold-anchor-import"
    result = cold_intake.run_recommend(
        db, {"journey": "cool-J1", "obtainable": {}}
    )
    assert result["recommendation"] == "none"


# ---------------------------------------------------------------------------
# Idempotency, originals and regression
# ---------------------------------------------------------------------------

def test_duplicate_imports_do_not_duplicate_evidence(db):
    seed_recorders(db)
    anchor = load("cold_anchor_minus_one.json")
    cold_intake.run_anchor(db, anchor)
    decisions_before = db.execute("SELECT COUNT(*) FROM decision").fetchone()[0]
    anchors_before = db.execute("SELECT COUNT(*) FROM cold_anchor").fetchone()[0]
    again = cold_intake.run_anchor(db, load("cold_anchor_minus_one.json"))
    assert again["status"] == "duplicate"
    assert db.execute("SELECT COUNT(*) FROM cold_anchor").fetchone()[0] == anchors_before
    assert db.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == decisions_before

    cold_intake.run_seal(db, load("cold_seal_rear.json"))
    again_seal = cold_intake.run_seal(db, load("cold_seal_rear.json"))
    assert again_seal["status"] == "duplicate"
    assert db.execute("SELECT COUNT(*) FROM cold_seal").fetchone()[0] == 2

    with connect(":memory:") as reset_db:
        seed_recorders(reset_db)
        cold_intake.run_reset(reset_db, load("cold_reset.json"))
        again_reset = cold_intake.run_reset(reset_db, load("cold_reset.json"))
        assert again_reset["status"] == "duplicate"
        assert reset_db.execute("SELECT COUNT(*) FROM cold_reset_visa").fetchone()[0] == 1


def test_originals_remain_reinterpretable_after_conflict(db):
    seed_recorders(db)
    cold_intake.run_anchor(db, load("cold_anchor_minus_one.json"))
    conflict = load("cold_seal_rear.json")
    conflict["source_id"] = "rear-seal-conflict"
    cold_intake.run_seal(db, conflict)
    assert history(db, "cool-J1")[-1][0] == "evidence_conflict"

    # Re-issuing the anchor work order re-adjudicates from the same bytes;
    # the recorder originals and both witness sets are all still present.
    snapshot = collect_evidence(db, "cool-J1")
    assert len(snapshot["recorders"]) == 2
    assert len(snapshot["anchors"]) == 2
    assert len(snapshot["seals"]) == 2
    result = cold_intake.run_attribute(db, {"journey": "cool-J1"})
    assert result["status"] == "evidence_conflict"


def test_market_ledgers_still_regress_green(tmp_path):
    from marketproof import quote, grade, trade, index
    database = connect(tmp_path / "case.sqlite")
    with database:
        quote.run(database, load("quote_valid.json"))
        grade.run(database, load("grade_valid.json"))
        trade.run(database, load("trade_valid.json"))
        index.run(database, load("index_valid.json"))
    latest = database.execute(
        "SELECT status,stall_count FROM index_release ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert (latest["status"], latest["stall_count"]) == ("published", 3)
    database.close()


def test_cli_offline_entries_are_registered(tmp_path, monkeypatch, capsys):
    from marketproof import __main__

    db_path = tmp_path / "cli.sqlite"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "marketproof",
            "cold-import",
            "--db",
            str(db_path),
            "--input",
            str(FIXTURES / "cold_first.json"),
        ],
    )
    __main__.main()
    out = capsys.readouterr().out
    assert '"status": "clear"' in out
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "marketproof",
            "cold-attribute",
            "--db",
            str(db_path),
            "--input",
            str(FIXTURES / "cold_anchor_minus_one.json"),
        ],
    )
    # cold-attribute ignores witness bytes and reads preserved originals.
    __main__.main()
    out = capsys.readouterr().out
    assert "alarm_unattributed" in out or "no_evidence" in out
