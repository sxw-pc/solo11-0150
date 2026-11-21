"""Offline intake for synthetic cold transport logs and attribution evidence.

Every intake first preserves the original bytes through :func:`ingest` and
only then runs attribution.  Legacy short-form recorder payloads keep their
original interface (:func:`run`); the new witness work orders use separate
entry points and their own physical tables.
"""

from .store import ingest
from .cold_claim import (
    update_claim,
    attribute,
    recommend_work_order,
    DEFAULT_SEGMENT,
)


def run(db, data):
    """Legacy short-form recorder import; unchanged interface and verdict."""
    result = ingest(db, "cold", data)
    if result != "new":
        return {"status": result}
    return update_claim(db, data["journey"])


def _insert_anchors(db, journey, data):
    segment = data.get("segment", DEFAULT_SEGMENT)
    anchors = data["anchors"]
    if len(anchors) < 2:
        raise ValueError("dual time-sync anchor work order needs two anchors")
    pinned = {a["unified_time"] - a["local_time"] for a in anchors}
    for anchor in anchors:
        db.execute(
            "INSERT INTO cold_anchor(journey,segment,source_id,local_time,"
            "unified_time,offset_minutes,business_time) VALUES(?,?,?,?,?,?,?)",
            (
                journey,
                segment,
                data["source_id"],
                anchor["local_time"],
                anchor["unified_time"],
                anchor["unified_time"] - anchor["local_time"],
                int(data.get("business_time", 0)),
            ),
        )
    return segment, sorted(pinned)


def run_anchor(db, data):
    """Dual clock-anchor preservation for one device-run segment."""
    journey = data["journey"]
    result = ingest(db, "cold", data)
    if result != "new":
        return {"status": result}
    with db:
        segment, pinned = _insert_anchors(db, journey, data)
    out = attribute(db, journey)
    out["calibrated_segment"] = segment
    out["pinned_offsets"] = pinned
    return out


def run_seal(db, data):
    """Temperature-sensitive seal scans independently maintained at handover."""
    journey = data["journey"]
    result = ingest(db, "cold", data)
    if result != "new":
        return {"status": result}
    segment = data.get("segment", DEFAULT_SEGMENT)
    with db:
        for seal in data["seals"]:
            start, end = seal["alarm_range"]
            db.execute(
                "INSERT INTO cold_seal(journey,segment,seal_id,party,"
                "range_start,range_end,identity_ok,goods_ok,alarm_minutes,"
                "source_id,business_time) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    journey,
                    segment,
                    seal["seal_id"],
                    seal["party"],
                    start,
                    end,
                    1 if seal.get("identity_ok", True) else 0,
                    1 if seal.get("goods_ok", True) else 0,
                    end - start,
                    data["source_id"],
                    int(data.get("business_time", 0)),
                ),
            )
    return attribute(db, journey)


def run_reset(db, data):
    """Reset visa: after it arrives the run splits into two offset segments."""
    journey = data["journey"]
    result = ingest(db, "cold", data)
    if result != "new":
        return {"status": result}
    before = data.get("before_segment", DEFAULT_SEGMENT)
    after = data["after_segment"]
    with db:
        db.execute(
            "INSERT INTO cold_reset_visa(journey,before_segment,after_segment,"
            "reset_at,source_id,business_time) VALUES(?,?,?,?,?,?)",
            (
                journey,
                before,
                after,
                data["reset_at"],
                data["source_id"],
                int(data.get("business_time", 0)),
            ),
        )
    return attribute(db, journey)


def run_attribute(db, data):
    """Re-run attribution over the preserved originals without new evidence."""
    return attribute(db, data["journey"])


def run_recommend(db, data):
    """One-budget work-order recommendation against obtainable witness sets."""
    return recommend_work_order(db, data["journey"], data.get("obtainable", {}))
