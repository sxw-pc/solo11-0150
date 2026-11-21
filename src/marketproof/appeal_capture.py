"""Offline grading appeals.

Legacy packages keep the old behaviour: the letter is forwarded to the grade
ledger and same-time ties freeze the lot as disputed.  Lots that explicitly
opted into independent re-inspection (``independent_review`` on the lot
master) instead consume registered originals: the same-round readings, the
allowed calibration offsets, the grading thresholds and the preserved
references.  For those lots the capture delivers either a final grade that
the registered originals support, or a pending slip stating which reference
must still be supplemented.  Equivalent opinions are never resolved by
voting, lab name or upload order.
"""

import json

from . import grade
from .store import ingest, decide


def run(db, data):
    status = ingest(db, "appeal", data)
    if status != "new":
        return {"status": status}
    grade.ensure_review_schema(db)
    review = data.get("independent_review")
    inspection = data.get("inspection")
    lot_id = (review or {}).get("lot_id") or (inspection or {}).get("lot_id")
    if review and lot_id and grade.review_enabled(db, lot_id):
        return _run_review(db, data, review, inspection, lot_id)
    if inspection is None:
        return {"status": "ignored", "reason": "independent review not enabled for lot"}
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
    out = dict(entry)
    if review:
        out["review"] = "not_enabled"
    return out


# ---------------------------------------------------------------- registration

def _run_review(db, data, review, inspection, lot_id):
    if inspection:
        grade.run(
            db,
            {
                "source_id": data["source_id"] + "-grade",
                "business_time": data["business_time"],
                "inspections": [inspection],
            },
        )
    _register_review(db, data, review, lot_id)
    verification = review.get("verification")
    outcome = _apply_verification(db, data, lot_id, verification) if verification else None
    return _deliver(db, data, lot_id, inspection, outcome)


def _register_review(db, data, review, lot_id):
    sid = data["source_id"]
    if any(k in review for k in ("round", "offsets", "thresholds", "registered_range")):
        if not (review.get("round") and review.get("offsets") and review.get("thresholds")):
            decide(db, "review-context", lot_id, "rejected",
                   "context needs the round, its allowed offsets and the grading thresholds", [sid])
        else:
            ctx = (
                review["round"],
                json.dumps(sorted({int(o) for o in review["offsets"]})),
                json.dumps(_parse_thresholds(review["thresholds"]), sort_keys=True),
                json.dumps([int(x) for x in review["registered_range"]]) if review.get("registered_range") else None,
            )
            old = db.execute("SELECT * FROM review_context WHERE lot_id=?", (lot_id,)).fetchone()
            if old is None:
                db.execute(
                    "INSERT INTO review_context(lot_id,round,offsets,thresholds,registered_range,source_id,business_time) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (lot_id, *ctx, sid, int(data.get("business_time", 0))),
                )
            elif (old["round"], old["offsets"], old["thresholds"], old["registered_range"]) != ctx:
                decide(db, "review-context", lot_id, "conflict",
                       "a registered calibration round cannot be redefined", [sid])
    for sample in review.get("samples", []):
        _register_sample(db, lot_id, sample, sid)
    for ref in review.get("references", []):
        _register_reference(db, lot_id, ref, sid)


def _register_sample(db, lot_id, sample, sid):
    specimen = sample["specimen"]
    fields = (
        sample.get("id"), sample.get("round"), int(sample["reading"]),
        1 if sample.get("preserved") else 0,
        sample.get("operator"), sample.get("witness"), sample.get("claim"),
    )
    old = db.execute(
        "SELECT * FROM review_sample WHERE lot_id=? AND specimen=?", (lot_id, specimen)
    ).fetchone()
    if old is None:
        db.execute(
            "INSERT INTO review_sample(lot_id,specimen,sample_id,round,reading,preserved,operator,witness,claim,source_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (lot_id, specimen, *fields, sid),
        )
        return
    same = (old["round"], old["reading"], old["preserved"], old["operator"], old["witness"]) == fields[1:6]
    if not same:
        # A second form for the same specimen is the same physical sample, not
        # a replacement; a changed operator, witness or reading is an identity
        # conflict and the original registration stands.
        decide(db, "sample-identity", specimen, "conflict",
               "same source specimen re-registered with different operator, witness or reading", [sid])


def _register_reference(db, lot_id, ref, sid):
    fields = (
        ref.get("kind"),
        int(ref["true_value"]) if ref.get("true_value") is not None else None,
        int(ref["threshold"]) if ref.get("threshold") is not None else None,
        ref.get("round"),
        1 if ref.get("available", True) else 0,
        1 if ref.get("preserved", True) else 0,
    )
    old = db.execute(
        "SELECT * FROM review_reference WHERE lot_id=? AND reference_id=?", (lot_id, ref["id"])
    ).fetchone()
    if old is None:
        db.execute(
            "INSERT INTO review_reference(lot_id,reference_id,kind,true_value,threshold,round,available,preserved,source_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (lot_id, ref["id"], *fields, sid),
        )
    elif (old["kind"], old["true_value"], old["threshold"], old["round"], old["available"], old["preserved"]) != fields:
        decide(db, "reference-identity", ref["id"], "conflict",
               "a registered reference cannot be silently redefined", [sid])


# ---------------------------------------------------------------- verification

def _apply_verification(db, data, lot_id, verification):
    ctx, samples, refs = _load_state(db, lot_id)
    if ctx is None:
        return {"status": "rejected", "reason": "no_review_context"}
    vid = verification.get("id")
    day = int(verification.get("day", data.get("business_time", 0)))
    procedure = verification.get("procedure")
    if not vid or procedure not in ("absolute_reference", "paired_comparison"):
        return {"status": "rejected", "reason": "bad_verification"}
    if procedure == "absolute_reference" and not all(
            k in verification for k in ("round", "standard", "standard_reading")):
        return {"status": "rejected", "reason": "bad_verification"}
    if procedure == "paired_comparison" and not all(
            k in verification for k in ("sample", "sample_reading", "comparisons")):
        return {"status": "rejected", "reason": "bad_verification"}
    if db.execute("SELECT 1 FROM review_verification WHERE lot_id=? AND verification_id=?",
                  (lot_id, vid)).fetchone():
        return {"status": "duplicate", "verification_id": vid}
    used = db.execute("SELECT verification_id FROM review_verification WHERE lot_id=? AND day=?",
                      (lot_id, day)).fetchone()
    if used:
        return {"status": "rejected", "reason": "verification_round_used",
                "day": day, "used_by": used["verification_id"]}
    if procedure == "absolute_reference":
        outcome = _eval_absolute(verification, ctx, samples, refs)
    else:
        outcome = _eval_paired(verification, ctx, samples, refs)
    if outcome["status"] == "rejected":
        return outcome
    db.execute(
        "INSERT INTO review_verification(lot_id,verification_id,day,procedure,round,payload,status,grade,conclusion,witness) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (lot_id, vid, day, procedure, verification.get("round"),
         json.dumps(verification, sort_keys=True, ensure_ascii=False),
         outcome["status"], outcome.get("grade"),
         json.dumps(outcome.get("conclusion", {}), sort_keys=True, ensure_ascii=False),
         verification.get("witness")),
    )
    _update_decision(db, lot_id, vid, day, outcome, ctx, samples)
    return outcome


def _eval_absolute(verification, ctx, samples, refs):
    thresholds = json.loads(ctx["thresholds"])
    offsets = json.loads(ctx["offsets"])
    rng = json.loads(ctx["registered_range"]) if ctx["registered_range"] else None
    if not verification.get("witness"):
        return {"status": "rejected", "reason": "missing_independent_witness"}
    if verification.get("round") != ctx["round"]:
        return {"status": "rejected", "reason": "round_mismatch",
                "detail": "another round's scale cannot sign the registered round"}
    std = next((r for r in refs if r["reference_id"] == verification.get("standard")), None)
    if std is None or std["kind"] != "standard" or not std["available"]:
        return {"status": "rejected", "reason": "standard_unavailable"}
    if std["round"] != ctx["round"]:
        return {"status": "rejected", "reason": "round_mismatch",
                "detail": "the standard belongs to another round's scale"}
    reading = int(verification["standard_reading"])
    offset = reading - int(std["true_value"])
    if (rng and not (rng[0] <= reading <= rng[1])) or offset not in offsets:
        return {"status": "rejected", "reason": "out_of_registered_range"}
    round_samples = [s for s in samples if s["round"] == ctx["round"]]
    if not round_samples:
        return {"status": "rejected", "reason": "no_sample_on_registered_round"}
    trues = {s["specimen"]: s["reading"] - offset for s in round_samples}
    grades = {sp: _grade_of(t, thresholds) for sp, t in trues.items()}
    unique = set(grades.values())
    if len(unique) == 1 and None not in unique:
        return {"status": "resolved", "procedure": "absolute_reference", "grade": unique.pop(),
                "conclusion": {"offset": offset, "true_values": trues, "grades": grades}}
    return {"status": "inconsistent",
            "conclusion": {"offset": offset, "true_values": trues, "grades": grades},
            "reason": "original samples disagree under the established offset"}


def _eval_paired(verification, ctx, samples, refs):
    thresholds = json.loads(ctx["thresholds"])
    if not verification.get("witness"):
        return {"status": "rejected", "reason": "missing_independent_witness"}
    if not verification.get("order_preserved", True):
        return {"status": "rejected", "reason": "order_not_preserved"}
    key = verification.get("sample") or verification.get("specimen")
    sample = next((s for s in samples if key in (s["specimen"], s["sample_id"])), None)
    if sample is None or not sample["preserved"]:
        return {"status": "rejected", "reason": "sample_not_preserved"}
    comparisons = verification.get("comparisons") or []
    if not comparisons:
        return {"status": "rejected", "reason": "no_threshold_comparison"}
    sample_reading = int(verification["sample_reading"])
    lower = upper = None
    used = []
    for comp in comparisons:
        ref = next((r for r in refs if r["reference_id"] == comp.get("reference")), None)
        if ref is None or ref["kind"] != "threshold" or not ref["available"] or not ref["preserved"]:
            return {"status": "rejected", "reason": "threshold_reference_unavailable",
                    "reference": comp.get("reference")}
        t = int(ref["threshold"])
        below = sample_reading < int(comp["reading"])
        used.append({"reference": ref["reference_id"], "threshold": t, "sample_below_reference": below})
        if below:
            upper = t if upper is None else min(upper, t)
        else:
            lower = t if lower is None else max(lower, t)
    if lower is not None and upper is not None and lower >= upper:
        return {"status": "rejected", "reason": "inconsistent_comparison"}
    remaining = _band_grades(thresholds, lower, upper)
    conclusion = {"lower_bound": lower, "upper_bound": upper, "comparisons": used}
    if len(remaining) == 1:
        return {"status": "resolved", "procedure": "paired_comparison", "grade": remaining[0],
                "conclusion": conclusion, "specimen": sample["specimen"]}
    return {"status": "partial", "conclusion": conclusion,
            "partial": {"excluded": [g for g in _grade_order(thresholds) if g not in remaining],
                        "remaining": remaining}}


def _update_decision(db, lot_id, vid, day, outcome, ctx, samples):
    current = db.execute("SELECT * FROM review_decision WHERE lot_id=?", (lot_id,)).fetchone()
    if current and current["status"] == "disputed":
        return  # genuine contradiction on file; later rounds cannot pick a side
    offsets = json.loads(ctx["offsets"])
    thresholds = json.loads(ctx["thresholds"])
    round_samples = [s for s in samples if s["round"] == ctx["round"]]
    compatible = _compatible_grades(round_samples, offsets, thresholds)
    if outcome["status"] == "resolved":
        if compatible and outcome["grade"] not in compatible:
            grade.apply_review_result(
                db, lot_id, None, "disputed",
                "verification contradicts the registered original readings; keeping common evidence",
                [vid, "registered-originals"], day)
            return
        others = db.execute(
            "SELECT verification_id,grade FROM review_verification "
            "WHERE lot_id=? AND status='resolved' AND verification_id!=?",
            (lot_id, vid)).fetchall()
        contradicting = [r["verification_id"] for r in others if r["grade"] != outcome["grade"]]
        if contradicting:
            evidence = sorted(set(contradicting) | {vid})
            grade.apply_review_result(
                db, lot_id, None, "disputed",
                "genuine verification rounds contradict on the preserved original sample; keeping common evidence",
                evidence, day)
        elif not (current and current["status"] == "resolved" and current["grade"] == outcome["grade"]):
            grade.apply_review_result(
                db, lot_id, outcome["grade"], "resolved",
                "independent re-inspection evidence signs the lot grade", [vid], day)
    elif outcome["status"] == "partial":
        if current and current["status"] == "resolved" and current["grade"] in outcome["partial"]["excluded"]:
            evidence = sorted(set(json.loads(current["evidence"])) | {vid})
            grade.apply_review_result(
                db, lot_id, None, "disputed",
                "later genuine comparison excludes the signed grade; keeping common evidence",
                evidence, day)


# ---------------------------------------------------------------- delivery

def _deliver(db, data, lot_id, inspection, outcome):
    ctx, samples, refs = _load_state(db, lot_id)
    if ctx is None:
        return {"status": "rejected", "reason": "no_review_context", "lot_id": lot_id}
    thresholds = json.loads(ctx["thresholds"])
    offsets = json.loads(ctx["offsets"])
    round_samples = [s for s in samples if s["round"] == ctx["round"]]
    compatible = _compatible_grades(round_samples, offsets, thresholds)
    decision = db.execute("SELECT * FROM review_decision WHERE lot_id=?", (lot_id,)).fetchone()
    entry = db.execute("SELECT grade, status FROM grade_decision WHERE lot_id=?", (lot_id,)).fetchone()
    base = {
        "lot_id": lot_id,
        "compatible_grades": compatible,
        "explanations": _explanations(round_samples, offsets, thresholds),
        "unsupported_claims": _unsupported_claims(inspection, round_samples, offsets, thresholds, compatible),
        "grade": entry["grade"] if entry else None,
        "lot_status": entry["status"] if entry else None,
    }
    if outcome and outcome["status"] == "duplicate":
        return {"status": "duplicate_verification", **base, "verification_id": outcome["verification_id"]}
    if outcome and outcome["status"] == "rejected":
        out = {"status": "rejected", **base, **_slip(db, data, lot_id, ctx, round_samples, refs, offsets, thresholds),
               "reason": outcome["reason"]}
        for key in ("detail", "day", "used_by", "reference"):
            if key in outcome:
                out[key] = outcome[key]
        return out
    if decision and decision["status"] == "disputed":
        return {"status": "disputed", **base,
                "kept_evidence": json.loads(decision["evidence"]), "reason": decision["reason"]}
    if decision and decision["status"] == "resolved":
        return {"status": "resolved", **base,
                "grade": decision["grade"], "evidence": json.loads(decision["evidence"])}
    out = {"status": "pending_evidence", **base, **_slip(db, data, lot_id, ctx, round_samples, refs, offsets, thresholds)}
    if outcome and outcome["status"] == "partial":
        out["partial"] = outcome["partial"]
    if outcome and outcome["status"] == "inconsistent":
        out["inconsistent"] = outcome["conclusion"]
    return out


def _slip(db, data, lot_id, ctx, round_samples, refs, offsets, thresholds):
    plan = _plan(ctx, round_samples, refs, offsets, thresholds)
    used = db.execute("SELECT COUNT(*) AS n FROM review_verification WHERE lot_id=? AND day=?",
                      (lot_id, int(data.get("business_time", 0)))).fetchone()
    return {
        "plan": plan,
        "needed": _needed(ctx, round_samples, refs, offsets, thresholds, plan),
        "verification_budget": {"day": int(data.get("business_time", 0)),
                                "rounds_per_day": 1, "used": used["n"]},
    }


def _plan(ctx, round_samples, refs, offsets, thresholds):
    rng = json.loads(ctx["registered_range"]) if ctx["registered_range"] else None
    preserved = [s for s in round_samples if s["preserved"]]
    plans = []
    stds = [r for r in refs if r["kind"] == "standard" and r["available"]]
    same_round = [r for r in stds if r["round"] == ctx["round"]]
    observations = []
    for std in same_round:
        for o in offsets:
            reading = int(std["true_value"]) + o
            if rng and not (rng[0] <= reading <= rng[1]):
                continue
            grades = {s["specimen"]: _grade_of(s["reading"] - o, thresholds) for s in round_samples}
            unique = set(grades.values())
            agree = len(unique) == 1 and None not in unique
            observations.append({
                "reference": std["reference_id"], "standard_reading": reading, "offset": o,
                "conclusion": {"grade": unique.pop()} if agree else {"status": "inconsistent", "grades": grades},
            })
    notes = []
    if stds and not same_round:
        notes.append("available standards belong to another round; today's scale cannot sign the registered round")
    plans.append({
        "procedure": "absolute_reference",
        "feasible": bool(observations),
        "guarantees_grade": bool(observations) and all("grade" in ob["conclusion"] for ob in observations),
        "observations": observations,
        "notes": notes,
    })
    thresh_refs = sorted(
        (r for r in refs if r["kind"] == "threshold" and r["available"] and r["preserved"]),
        key=lambda r: -int(r["threshold"]))
    bands = _plan_bands(round_samples, offsets, thresholds, [int(r["threshold"]) for r in thresh_refs])
    notes = []
    if not preserved:
        notes.append("no preserved original sample; paired comparison cannot run")
    plans.append({
        "procedure": "paired_comparison",
        "feasible": bool(preserved) and bool(thresh_refs),
        "guarantees_grade": bool(preserved) and bool(bands) and all(len(b["grades"]) == 1 for b in bands),
        "observations": [{"band": [b["lower"], b["upper"]], "conclusion": {"grades": b["grades"]}}
                         for b in bands],
        "notes": notes,
    })
    return plans


def _needed(ctx, round_samples, refs, offsets, thresholds, plan):
    needed = []
    preserved = [s for s in round_samples if s["preserved"]]
    if not round_samples:
        needed.append({"kind": "original_sample",
                       "detail": "no original sample registered on the round's scale"})
    elif not preserved:
        needed.append({"kind": "preserved_sample",
                       "detail": "no preserved original sample for paired comparison"})
    have = {int(r["threshold"]) for r in refs
            if r["kind"] == "threshold" and r["available"] and r["preserved"]}
    extra = []
    while True:
        bands = _plan_bands(round_samples, offsets, thresholds, sorted(have | set(extra)))
        multi = [b for b in bands if len(b["grades"]) > 1]
        if not multi:
            break
        band = multi[0]
        candidates = [t for t in thresholds.values()
                      if (band["lower"] is None or t > band["lower"])
                      and (band["upper"] is None or t < band["upper"])
                      and t not in have and t not in extra]
        if not candidates:
            break
        extra.append(max(candidates))
    for t in extra:
        needed.append({"kind": "threshold_reference", "threshold": t})
    absolute = next(p for p in plan if p["procedure"] == "absolute_reference")
    if not absolute["observations"]:
        needed.append({"kind": "standard", "round": ctx["round"],
                       "detail": "same-round absolute reference"})
    return needed


# ---------------------------------------------------------------- shared maths

def _load_state(db, lot_id):
    ctx = db.execute("SELECT * FROM review_context WHERE lot_id=?", (lot_id,)).fetchone()
    samples = db.execute("SELECT * FROM review_sample WHERE lot_id=? ORDER BY specimen", (lot_id,)).fetchall()
    refs = db.execute("SELECT * FROM review_reference WHERE lot_id=? ORDER BY reference_id", (lot_id,)).fetchall()
    return ctx, samples, refs


def _parse_thresholds(raw):
    out = {}
    for name, value in raw.items():
        g = name[:-4] if name.endswith("_min") else name
        out[g] = int(value)
    return out


def _grade_order(thresholds):
    return [g for g, _ in sorted(thresholds.items(), key=lambda kv: -kv[1])]


def _grade_of(true_value, thresholds):
    for g, t in sorted(thresholds.items(), key=lambda kv: -kv[1]):
        if true_value >= t:
            return g
    return None


def _explanations(samples, offsets, thresholds):
    # One shared calibration curve per round: a single offset explains every
    # original sample at once; per-sample offsets are never offered.
    return [{"offset": o, "grades": {s["specimen"]: _grade_of(s["reading"] - o, thresholds) for s in samples}}
            for o in offsets]


def _compatible_grades(samples, offsets, thresholds):
    out = set()
    for s in samples:
        for o in offsets:
            g = _grade_of(s["reading"] - o, thresholds)
            if g:
                out.add(g)
    return sorted(out)


def _unsupported_claims(inspection, samples, offsets, thresholds, compatible):
    claims = []
    for s in samples:
        if s["claim"]:
            own = {_grade_of(s["reading"] - o, thresholds) for o in offsets} - {None}
            if s["claim"] not in own:
                claims.append({"sample": s["sample_id"], "claim": s["claim"]})
    if inspection and inspection.get("grade") and inspection["grade"] not in compatible:
        claims.append({"inspection": inspection["id"], "claim": inspection["grade"]})
    return claims


def _band_grades(thresholds, lower, upper):
    # Grades whose [min, next-min) band overlaps the compared bounds.  Only
    # boundary information is used; no absolute reading is fabricated.
    order = sorted(thresholds.items(), key=lambda kv: -kv[1])
    out = []
    for i, (g, t) in enumerate(order):
        u = order[i - 1][1] if i else None
        if upper is not None and t >= upper:
            continue
        if lower is not None and u is not None and u <= lower:
            continue
        out.append(g)
    return out


def _plan_bands(samples, offsets, thresholds, ref_thresholds):
    if not samples:
        return []
    lo = min(s["reading"] for s in samples) - max(offsets)
    hi = max(s["reading"] for s in samples) - min(offsets)
    points = sorted({int(t) for t in ref_thresholds}, reverse=True)
    intervals = []
    prev = None
    for p in points:
        intervals.append((p, prev))
        prev = p
    intervals.append((None, prev))
    bands = []
    for lower, upper in intervals:
        if upper is not None and upper <= lo:
            continue
        if lower is not None and lower > hi:
            continue
        bands.append({"lower": lower, "upper": upper, "grades": _band_grades(thresholds, lower, upper)})
    return bands
