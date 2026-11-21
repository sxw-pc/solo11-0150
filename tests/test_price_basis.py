"""Acceptance for evidence-based offline price capture (price basis).

The counterexample comes from fixtures/price_slip_uncertain.json: a folded
sign shows "12"; the signed basket 3A + 2B settled at 13, which both
A=1/B=5 and A=3/B=2 explain, and the stall notice wants a quote for A.
"""
from pathlib import Path

from marketproof import price_slip, quote
from marketproof.store import connect, read

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
UNCERTAIN = read(FIXTURES / "price_slip_uncertain.json")

A, B = "tomato", "cucumber"          # A is the row the stall notice wants to price
PRODUCTS = [A, B]
ENTRY = UNCERTAIN["quote"]["entry"]           # q-slip, lic-1, stall-1, day 10, version 2
BASKET = UNCERTAIN["arbitration_baskets"][0]  # 3A + 2B settled at 13
PAIRS = UNCERTAIN["possible_unit_pairs"]      # [[1, 5], [3, 2]]
SECOND = UNCERTAIN["second_basket"]           # 2A + 3B reads 17 or 12
LICENCE_FOR = {A: "lic-1", B: "lic-4"}


def licences(status="active"):
    data = read(FIXTURES / "quote_valid.json")
    for lic in data["licences"]:
        lic["status"] = status
    data["licences"].append({"id": "lic-4", "stall": "stall-1", "region": "north",
                             "product": B, "starts": 0, "ends": 30, "status": status})
    return data


def seed(db, status="active"):
    quote.run(db, licences(status))


def request(product, qid, version=2):
    return dict(ENTRY, id=qid, product=product, version=version,
                licence_id=LICENCE_FOR[product])


def ticket(tid, quantities, total, signed=True):
    return {"id": tid, "quantities": quantities, "total": total, "signed": signed}


def marked(mid, product, quantity, price, sheet_version=2, signed=True):
    return {"id": mid, "product": product, "quantity": quantity, "price": price,
            "row": "R3", "sheet": "sheet-9", "sheet_version": sheet_version,
            "signed": signed}


def capture(sid, *, requests, marked_rows=(), tickets=(), convention="total",
            deposit=0, available_rows=(), trial_scale=True, batch="batch-1"):
    return {
        "source_id": sid,
        "business_time": 10,
        "capture": {
            "stall": ENTRY["stall"], "day": ENTRY["day"],
            "batch": {"id": batch, "convention": convention, "deposit": deposit},
            "products": PRODUCTS,
            "requests": requests,
            "marked_rows": list(marked_rows),
            "trial_tickets": list(tickets),
            "availability": {"marked_rows": list(available_rows),
                             "trial_scale": trial_scale},
        },
    }


def test_common_candidates_hold_requested_row_until_disambiguated():
    with connect(":memory:") as db:
        seed(db)
        first = price_slip.run(db, capture(
            "cap-1", requests=[request(A, "q-slip")],
            tickets=[ticket("t-1", {A: BASKET["quantities"][0], B: BASKET["quantities"][1]},
                            BASKET["sum"])]))
        assert first["batch_status"] == "open"
        assert first["candidates"] == [dict(zip(PRODUCTS, pair)) for pair in PAIRS]
        assert first["unit_prices"] == {A: None, B: None}
        assert first["issued"] == []
        assert [h["quote"] for h in first["held"]] == ["q-slip"]
        row = db.execute("SELECT status, price FROM quote_revision WHERE id='q-slip'").fetchone()
        assert (row["status"], row["price"]) == ("held", None)
        # re-reading the same 3A + 2B basket adds no evidence
        again = price_slip.run(db, capture(
            "cap-2", requests=[request(A, "q-slip")],
            tickets=[ticket("t-2", {A: 3, B: 2}, 13)]))
        assert again["candidates"] == first["candidates"]
        rec = again["recommendation"]
        assert rec["kind"] == "trial"
        assert rec["quantities"] != {A: 3, B: 2}


def test_marked_sheet_recapture_proves_both_rows_without_reweighing():
    with connect(":memory:") as db:
        seed(db)
        req = [request(A, "q-slip"), request(B, "q-slip-b")]
        first = price_slip.run(db, capture(
            "cap-1", requests=req, tickets=[ticket("t-1", {A: 3, B: 2}, 13)],
            available_rows=[{"product": A, "row": "R3", "sheet": "sheet-9",
                             "sheet_version": 2}]))
        assert first["recommendation"]["kind"] == "marked"
        assert first["recommendation"]["product"] == A
        second = price_slip.run(db, capture(
            "cap-2", requests=req, marked_rows=[marked("m-1", A, 1, 3)]))
        # A is verified on the actually read marked row; B follows from the
        # shared basis without weighing another basket
        assert second["unit_prices"] == {A: 3, B: 2}
        assert {(q["quote"], q["status"], q["price"]) for q in second["issued"]} == {
            ("q-slip", "valid", 3), ("q-slip-b", "valid", 2)}
        assert second["recommendation"] is None


def test_trial_purchase_path_when_marked_sheet_is_damaged():
    for reading, expected in zip(SECOND["results"], PAIRS):
        with connect(":memory:") as db:
            seed(db)
            req = [request(A, "q-slip"), request(B, "q-slip-b")]
            first = price_slip.run(db, capture(
                "cap-1", requests=req, tickets=[ticket("t-1", {A: 3, B: 2}, 13)],
                available_rows=(), trial_scale=True))   # upper half of the sheet is damaged
            rec = first["recommendation"]
            assert rec["kind"] == "trial"
            # one basket, computed from the undecided originals: every reading
            # settles every candidate, so no second trial purchase is needed
            assert len({e["total"] for e in rec["expectations"]}) == len(PAIRS)
            assert rec["quantities"] != {A: 3, B: 2}
            qty = SECOND["quantities"]
            second = price_slip.run(db, capture(
                "cap-2", requests=req,
                tickets=[ticket("t-2", {A: qty[0], B: qty[1]}, reading)]))
            assert second["candidates"] == [dict(zip(PRODUCTS, expected))]
            # both rows can be issued at once from the settled basis
            assert {(q["quote"], q["price"]) for q in second["issued"]} == {
                ("q-slip", expected[0]), ("q-slip-b", expected[1])}


def test_marked_recapture_recommended_when_trial_scale_unavailable():
    with connect(":memory:") as db:
        seed(db)
        out = price_slip.run(db, capture(
            "cap-1", requests=[request(A, "q-slip")],
            tickets=[ticket("t-1", {A: 3, B: 2}, 13)],
            available_rows=[{"product": A, "row": "R3", "sheet": "sheet-9",
                             "sheet_version": 2}],
            trial_scale=False))
        assert out["recommendation"]["kind"] == "marked"
        neither = price_slip.run(db, capture(
            "cap-2", requests=[request(A, "q-slip")],
            tickets=[ticket("t-2", {A: 3, B: 2}, 13)],
            available_rows=(), trial_scale=False))
        assert neither["recommendation"]["kind"] == "none"


def test_row_with_independent_evidence_not_blocked_by_unknown_sibling():
    with connect(":memory:") as db:
        seed(db)
        out = price_slip.run(db, capture(
            "cap-1", requests=[request(A, "q-slip"), request(B, "q-slip-b")],
            marked_rows=[marked("m-1", A, 1, 1)],
            tickets=[ticket("t-1", {A: 1, B: 0.5}, 3)]))
        assert out["candidates"] == [{A: 1, B: 3}, {A: 1, B: 4}]
        assert out["unit_prices"] == {A: 1, B: None}
        assert {(q["quote"], q["status"], q["price"]) for q in out["issued"]} == {
            ("q-slip", "valid", 1)}
        assert [h["quote"] for h in out["held"]] == ["q-slip-b"]
        assert [m["product"] for m in out["missing"]] == [B]


def test_per_line_rounding_keeps_cumulative_difference_and_deposit():
    with connect(":memory:") as db:
        seed(db)
        out = price_slip.run(db, capture(
            "cap-1", requests=[request(A, "q-slip"), request(B, "q-slip-b")],
            marked_rows=[marked("m-1", B, 1, 3)],
            tickets=[ticket("t-1", {A: 0.5, B: 0.5}, 6),   # per-line 2 + 2, deposit 2
                     ticket("t-2", {A: 1, B: 1}, 8)],       # 3 + 3, deposit 2
            convention="per_line", deposit=2))
        assert out["batch_status"] == "open"
        # the exact sum of t-1 is 3.0; the signed 6 keeps the per-line
        # cumulative difference and the final change instead of losing them
        assert out["candidates"] == [{A: 3, B: 3}]
        assert {(q["quote"], q["price"]) for q in out["issued"]} == {
            ("q-slip", 3), ("q-slip-b", 3)}


def test_total_convention_flags_cumulative_rounding_contradiction():
    with connect(":memory:") as db:
        seed(db)
        out = price_slip.run(db, capture(
            "cap-1", requests=[request(A, "q-slip")],
            marked_rows=[marked("m-1", A, 1, 3), marked("m-2", B, 1, 3)],
            tickets=[ticket("t-1", {A: 0.5, B: 0.5}, 4)],  # exact sum is 3.0
            convention="total"))
        assert out["batch_status"] == "contradicted"
        assert out["candidates"] == []
        assert out["issued"] == []
        assert [h["quote"] for h in out["held"]] == ["q-slip"]
        batch = db.execute("SELECT status FROM slip_batch WHERE id='batch-1'").fetchone()
        assert batch["status"] == "contradicted"


def test_late_receipt_exposes_basis_conflict_retains_issued_blocks_new():
    with connect(":memory:") as db:
        seed(db)
        req = [request(A, "q-slip"), request(B, "q-slip-b")]
        first = price_slip.run(db, capture(
            "cap-1", requests=req, marked_rows=[marked("m-1", A, 1, 1)],
            tickets=[ticket("t-1", {A: 3, B: 2}, 13)]))
        assert {(q["quote"], q["price"]) for q in first["issued"]} == {
            ("q-slip", 1), ("q-slip-b", 5)}
        late = price_slip.run(db, capture(
            "cap-late", requests=[request(A, "q-slip-c", version=3)],
            tickets=[ticket("t-2", {A: 2, B: 3}, 12)]))   # only A=3/B=2 reads 12
        assert late["batch_status"] == "contradicted"
        assert [h["quote"] for h in late["held"]] == ["q-slip-c"]
        assert late["recommendation"]["kind"] == "none"
        # historically issued vouchers are retained as certified
        kept = db.execute("SELECT status, price FROM quote_revision WHERE id='q-slip'").fetchone()
        assert (kept["status"], kept["price"]) == ("valid", 1)
        kept_b = db.execute("SELECT status, price FROM quote_revision WHERE id='q-slip-b'").fetchone()
        assert (kept_b["status"], kept_b["price"]) == ("valid", 5)


def test_existing_version_cannot_be_split_and_replay_is_idempotent():
    with connect(":memory:") as db:
        seed(db)
        legacy = price_slip.run(db, read(FIXTURES / "price_slip_uncertain.json"))
        assert legacy == {"quotes": [{"quote": "q-slip", "status": "valid"}]}
        first = price_slip.run(db, capture(
            "cap-1", requests=[request(A, "q-slip")],
            marked_rows=[marked("m-1", A, 1, 3)], tickets=[ticket("t-1", {A: 3, B: 2}, 13)]))
        assert first["unit_prices"] == {A: 3, B: 2}
        # the bare 12 already occupies q-slip version 2 and cannot be split
        assert [b["quote"] for b in first["version_blocked"]] == ["q-slip"]
        assert first["version_blocked"][0]["existing_price"] == 12
        assert first["issued"] == []
        assert first["recommendation"]["kind"] == "reissue"
        kept = db.execute("SELECT price, status FROM quote_revision WHERE id='q-slip'").fetchone()
        assert (kept["price"], kept["status"]) == (12, "valid")
        second = price_slip.run(db, capture(
            "cap-2", requests=[request(A, "q-slip-v3", version=3)]))
        assert second["issued"] == [
            {"quote": "q-slip-v3", "product": A, "status": "valid", "price": 3}]
        counts = lambda: tuple(db.execute(
            "SELECT (SELECT COUNT(*) FROM quote_revision), (SELECT COUNT(*) FROM decision)").fetchone())
        before = counts()
        assert price_slip.run(db, read(FIXTURES / "price_slip_uncertain.json")) == {"status": "duplicate"}
        assert price_slip.run(db, capture(
            "cap-2", requests=[request(A, "q-slip-v3", version=3)])) == {"status": "duplicate"}
        assert counts() == before
        third = price_slip.run(db, capture(
            "cap-3", requests=[request(A, "q-slip-v3", version=3)],
            marked_rows=[marked("m-1", A, 1, 3)], tickets=[ticket("t-1", {A: 3, B: 2}, 13)]))
        assert third["issued"] == [
            {"quote": "q-slip-v3", "product": A, "status": "duplicate", "price": 3}]


def test_licence_still_adjudicates_derived_quotes():
    for status, expected in (("active", "valid"), ("suspended", "unauthorized")):
        with connect(":memory:") as db:
            seed(db, status)
            out = price_slip.run(db, capture(
                "cap-1", requests=[request(A, "q-slip")],
                marked_rows=[marked("m-1", A, 1, 3)],
                tickets=[ticket("t-1", {A: 3, B: 2}, 13)]))
            assert out["issued"] == [
                {"quote": "q-slip", "product": A, "status": expected, "price": 3}]


def test_unsigned_combination_cannot_impersonate_a_signed_original():
    with connect(":memory:") as db:
        seed(db)
        out = price_slip.run(db, capture(
            "cap-1", requests=[request(A, "q-slip")],
            tickets=[ticket("t-1", {A: 3, B: 2}, 13),
                     ticket("t-2", {A: 2, B: 3}, 17, signed=False)]))
        assert out["rejected_originals"] == ["t-2"]
        assert out["candidates"] == [dict(zip(PRODUCTS, pair)) for pair in PAIRS]
        assert [h["quote"] for h in out["held"]] == ["q-slip"]
        stored = db.execute("SELECT COUNT(*) FROM slip_original").fetchone()[0]
        assert stored == 1
        rec = db.execute("SELECT kind, status FROM slip_recommendation").fetchone()
        assert (rec["kind"], rec["status"]) == ("trial", "proposed")


def test_recommendation_is_computed_from_current_readings_not_a_fixed_recipe():
    with connect(":memory:") as db:
        seed(db)
        first = price_slip.run(db, capture(
            "cap-1", requests=[request(A, "q-slip")],
            tickets=[ticket("t-1", {A: 3, B: 2}, 13)]))
        rec1 = first["recommendation"]
        assert rec1["kind"] == "trial"
        assert len({e["total"] for e in rec1["expectations"]}) == 2
    with connect(":memory:") as db:
        seed(db)
        other = price_slip.run(db, capture(
            "cap-1", requests=[request(A, "q-slip")],
            tickets=[ticket("t-1", {A: 1, B: 1}, 6)]))
        # the buried settlement total 6 is not treated as a price-list row
        assert other["candidate_count"] == 5
        assert all(c[A] != 6 and c[B] != 6 for c in other["candidates"])
        assert other["unit_prices"] == {A: None, B: None}
        rec2 = other["recommendation"]
        assert rec2["kind"] == "trial"
        assert len({e["total"] for e in rec2["expectations"]}) == 5
        assert rec2["quantities"] != rec1["quantities"]


def test_redeclared_batch_convention_is_basis_mixing():
    with connect(":memory:") as db:
        seed(db)
        first = price_slip.run(db, capture(
            "cap-1", requests=[request(A, "q-slip")],
            tickets=[ticket("t-1", {A: 3, B: 2}, 13)]))
        assert first["batch_status"] == "open"
        mixed = price_slip.run(db, capture(
            "cap-2", requests=[request(A, "q-slip")],
            tickets=[ticket("t-2", {A: 2, B: 3}, 17)],
            convention="per_line"))          # same number string, other convention
        assert mixed["batch_status"] == "mixed"
        assert mixed["added_originals"] == []
        assert [h["quote"] for h in mixed["held"]] == ["q-slip"]
        assert mixed["recommendation"]["kind"] == "none"
        batch = db.execute("SELECT status, candidates FROM slip_batch WHERE id='batch-1'").fetchone()
        assert batch["status"] == "mixed"


def test_basis_review_lists_batches_originals_holds_and_proposals():
    with connect(":memory:") as db:
        seed(db)
        price_slip.run(db, capture(
            "cap-1", requests=[request(A, "q-slip")],
            tickets=[ticket("t-1", {A: 3, B: 2}, 13)]))
        view = price_slip.review(db)
        assert [b["id"] for b in view["slip_batch"]] == ["batch-1"]
        assert [o["id"] for o in view["slip_original"]] == ["t-1"]
        assert [r["kind"] for r in view["slip_recommendation"]] == ["trial"]
        assert [h["id"] for h in view["held_quotes"]] == ["q-slip"]
