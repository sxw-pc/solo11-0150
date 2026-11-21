"""First-pass contractual excursion threshold and attribution windows.

The original :func:`update_claim` short-form path only reads the aggregate
alarm total and never assigns a responsible party.  The attribution engine
below works from the preserved recorder originals plus two kinds of real
witness intake (dual clock anchors and temperature-sensitive seals).

Time model
----------
Within one segment of one device run, the recorder's local clock differs
from the unified clock by a single unknown constant offset.  A reset visa
splits the run into two segments, each with its own offset.  A single
continuous hot zone may span both custody parties; every alarm minute is
counted only for the party whose signed custody window contains it.  A
party reaches the liability threshold (3 minutes) only inside one
continuous hot zone — separated hot zones are never summed.
"""

import json

from .store import decide

DEFAULT_SEGMENT = "seg-1"


def update_claim(db, journey):
    rows = db.execute(
        "SELECT payload FROM source WHERE system='cold' ORDER BY business_time, id"
    )
    records = [json.loads(row["payload"]) for row in rows]
    relevant = [row for row in records if row["journey"] == journey]
    latest = relevant[-1]
    duration = latest.get("alarm_minutes")
    if duration is None:
        status = "missing_summary"
    elif duration >= latest["trigger_minutes"]:
        status = "alarm_unattributed"
    else:
        status = "clear"
    decide(db, "cold_claim", journey, status, "aggregate alarm summary", [latest["source_id"]])
    return {"journey": journey, "status": status}


# ---------------------------------------------------------------------------
# Evidence collection: everything is re-derived from preserved originals so
# that late evidence can always re-interpret the same recorder bytes.
# ---------------------------------------------------------------------------

def _recorders(db, journey):
    """Recorder short-form originals only.

    Anchor, seal and reset work orders are preserved under the same append-
    only ``cold`` source stream but lack the recorder geometry block, so they
    are distinguished structurally rather than by source priority.
    """
    rows = db.execute(
        "SELECT payload FROM source WHERE system='cold' ORDER BY business_time, id"
    )
    out = []
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("journey") != journey:
            continue
        if "recorder" not in payload or "custody_window" not in payload:
            continue
        out.append(payload)
    return out


def _segments(db, journey):
    rows = db.execute(
        "SELECT * FROM cold_reset_visa WHERE journey=? ORDER BY reset_at, id",
        (journey,),
    ).fetchall()
    segments = [DEFAULT_SEGMENT]
    for row in rows:
        after = row["after_segment"]
        if after not in segments:
            segments.append(after)
    return segments, rows


def _anchors(db, journey):
    return db.execute(
        "SELECT * FROM cold_anchor WHERE journey=? ORDER BY business_time, id",
        (journey,),
    ).fetchall()


def _seals(db, journey):
    return db.execute(
        "SELECT * FROM cold_seal WHERE journey=? ORDER BY business_time, id",
        (journey,),
    ).fetchall()


def collect_evidence(db, journey):
    """Read-only snapshot of every original relevant to one journey."""
    recorders = _recorders(db, journey)
    segments, visas = _segments(db, journey)
    anchors = _anchors(db, journey)
    seals = _seals(db, journey)
    split_visas = len(visas) > 0
    return {
        "journey": journey,
        "recorders": recorders,
        "segments": segments,
        "reset_visas": [dict(v) for v in visas],
        "anchors": [dict(a) for a in anchors],
        "seals": [dict(s) for s in seals],
        "has_reset_visa": split_visas,
    }


def _recorder_for_segment(recorders, segment):
    """Latest recorder payload covering a segment.

    A payload names an explicit segment; legacy short-form payloads belong to
    the implicit single segment and never cross a reset boundary.  When both
    generations live in the same database, an explicit-segment match wins
    over legacy implicit-segment payloads.
    """
    explicit = [
        r for r in recorders if r.get("segment") == segment
    ]
    if explicit:
        return explicit[-1]
    if segment == DEFAULT_SEGMENT:
        legacy = [
            r for r in recorders
            if "segment" not in r and r.get("segment", DEFAULT_SEGMENT) == DEFAULT_SEGMENT
        ]
        if legacy:
            return legacy[-1]
    return None


# ---------------------------------------------------------------------------
# Offset and hot-zone geometry
# ---------------------------------------------------------------------------

def _legal_offsets(recorder, anchors, segment, split_visas):
    """Every integer offset still compatible with recorder bounds and anchors.

    Anchors must be compared on BOTH their local and unified times.  A
    whole-run anchor can only calibrate the implicit single segment; once a
    reset visa exists it cannot reach later generations.
    """
    lo, hi = recorder["recorder"].get("clock_offset_bounds", [-1, 1])
    offsets = set(range(lo, hi + 1))
    for anchor in anchors:
        anchor_segment = anchor["segment"]
        if anchor_segment is None:
            # Legacy whole-run anchor: only the implicit single segment may
            # use it, and never after a reset visa split.
            if split_visas or segment != DEFAULT_SEGMENT:
                continue
        elif anchor_segment != segment:
            continue
        pinned = anchor["unified_time"] - anchor["local_time"]
        if pinned != anchor["offset_minutes"]:
            # The two readings of one anchor contradict each other.
            return set()
        offsets &= {pinned}
    return offsets


def _party_minutes(hot_local, offset, split_at):
    """Continuous per-party minutes after shifting one hot zone by offset."""
    start, end = hot_local
    unified_start = start + offset
    unified_end = end + offset
    first = max(0, min(unified_end, split_at) - unified_start)
    second = max(0, unified_end - max(unified_start, split_at))
    return first, second


def _interpretations(recorder, offsets):
    """For every legal offset, per-party continuous minutes in the zone."""
    hot = recorder["recorder"]["hot_on_local_clock"]
    split_at = recorder["custody_window"]["split_at"]
    views = {}
    for offset in sorted(offsets):
        first, second = _party_minutes(hot, offset, split_at)
        views[offset] = {
            "offset": offset,
            "first_minutes": first,
            "second_minutes": second,
            "first_party": recorder["custody_window"]["first"],
            "second_party": recorder["custody_window"]["second"],
        }
    return views


# ---------------------------------------------------------------------------
# Seals: independent physical windows, no recorder offset recovery required
# ---------------------------------------------------------------------------

def _valid_seals(seals, segment):
    out = []
    for seal in seals:
        if seal["segment"] != segment:
            continue
        if seal["identity_ok"] and seal["goods_ok"]:
            out.append(seal)
    return out


def _seal_world_ok(views, seal, recorder):
    """World intersection: seal window inside recorder hot zone under some
    still-legal offset."""
    for view in views.values():
        shifted_start = recorder["recorder"]["hot_on_local_clock"][0] + view["offset"]
        shifted_end = recorder["recorder"]["hot_on_local_clock"][1] + view["offset"]
        if shifted_start <= seal["range_start"] and seal["range_end"] <= shifted_end:
            return True
    return False


def _counterparty_below_threshold(valid_seals, party, recorder):
    """The other custody party is already proven below the threshold by its
    own independently maintained seal."""
    other = (
        recorder["custody_window"]["second"]
        if party == recorder["custody_window"]["first"]
        else recorder["custody_window"]["first"]
    )
    trigger = recorder["trigger_minutes"]
    below = [
        s for s in valid_seals
        if s["party"] == other and s["alarm_minutes"] < trigger
    ]
    return bool(below), other


# ---------------------------------------------------------------------------
# One segment adjudication
# ---------------------------------------------------------------------------

def _adjudicate_segment(segment, evidence):
    recorder = _recorder_for_segment(evidence["recorders"], segment)
    if recorder is None:
        return None
    anchors = evidence["anchors"]
    seals = _valid_seals(evidence["seals"], segment)
    split_visas = evidence["has_reset_visa"]
    trigger = recorder["trigger_minutes"]
    first_party = recorder["custody_window"]["first"]
    second_party = recorder["custody_window"]["second"]

    legal = _legal_offsets(recorder, anchors, segment, split_visas)
    views = _interpretations(recorder, legal) if legal else {}

    anchor_seen = any(
        (a["segment"] == segment)
        or (a["segment"] is None and not split_visas and segment == DEFAULT_SEGMENT)
        for a in anchors
    )

    # Physical world intersection between seal testimony and recorder zones.
    # Only a seal strong enough to certify a party can contradict the
    # recorder worlds; a below-threshold counterparty seal is admissible on
    # its own monitored range without offset recovery.
    for seal in seals:
        if seal["alarm_minutes"] < trigger:
            continue
        if legal and not _seal_world_ok(views, seal, recorder):
            return {
                "segment": segment,
                "status": "evidence_conflict",
                "reason": "evidence conflict: seal window lies outside every legal recorder interpretation",
                "party": None,
                "seal": dict(seal),
                "legal_offsets": sorted(legal),
                "interpretations": views,
            }

    anchor_party = None
    if legal:
        first_all = all(v["first_minutes"] >= trigger for v in views.values())
        second_all = all(v["second_minutes"] >= trigger for v in views.values())
        if first_all and not second_all:
            anchor_party = first_party
        elif second_all and not first_all:
            anchor_party = second_party

    # A seal certifies its own party only when the counterparty is separately
    # proven below threshold; seals never pin a recorder offset.
    seal_party = None
    seal_evidence = None
    for seal in seals:
        if seal["alarm_minutes"] < trigger:
            continue
        below, other = _counterparty_below_threshold(seals, seal["party"], recorder)
        if not below:
            continue
        if seal_party is not None and seal_party != seal["party"]:
            return {
                "segment": segment,
                "status": "evidence_conflict",
                "reason": "seals certify both custody parties at once",
                "party": None,
                "interpretations": views,
            }
        seal_party = seal["party"]
        seal_evidence = seal

    if anchor_party and seal_party and anchor_party != seal_party:
        return {
            "segment": segment,
            "status": "evidence_conflict",
            "reason": "anchor calibration and seal testimony point at different parties",
            "party": None,
            "seal": dict(seal_evidence) if seal_evidence else None,
            "interpretations": views,
        }

    if anchor_party and seal_party and anchor_party == seal_party:
        party = anchor_party
        return {
            "segment": segment,
            "status": "attributed",
            "reason": "anchors and seals jointly certify the same party",
            "party": party,
            "interpretations": views,
            "seal": dict(seal_evidence),
        }

    if anchor_party:
        return {
            "segment": segment,
            "status": "attributed",
            "reason": "party reaches threshold under every legal common offset",
            "party": anchor_party,
            "interpretations": views,
        }

    if seal_party:
        # Deliberately no offset field: seal paths must not fill ±1 into the
        # archive, and the recorder alarm total stays untouched.
        return {
            "segment": segment,
            "status": "attributed",
            "reason": "independent seal proves a qualifying continuous alarm and counterparty is below threshold",
            "party": seal_party,
            "seal": dict(seal_evidence),
            "recorder_total": recorder.get("alarm_minutes"),
        }

    if anchor_seen and not legal:
        return {
            "segment": segment,
            "status": "evidence_conflict",
            "reason": "anchor pair has no offset compatible with recorder bounds",
            "party": None,
            "interpretations": {},
        }

    remaining = sorted(legal)
    return {
        "segment": segment,
        "status": "alarm_unattributed",
        "reason": "no party reaches threshold under all legal interpretations",
        "party": None,
        "legal_offsets": remaining,
        "interpretations": views,
        "seals": [dict(s) for s in seals],
    }


# ---------------------------------------------------------------------------
# Whole-journey attribution with reset generations
# ---------------------------------------------------------------------------

def _object_id(journey, segment, split_visas):
    if not split_visas and segment == DEFAULT_SEGMENT:
        return journey
    return f"{journey}:{segment}"


def attribute(db, journey):
    """Re-interpret all preserved originals and (re)issue liability verdicts."""
    evidence = collect_evidence(db, journey)
    split_visas = evidence["has_reset_visa"]
    verdicts = []
    statuses = []

    for segment in evidence["segments"]:
        verdict = _adjudicate_segment(segment, evidence)
        if verdict is None:
            continue
        object_id = _object_id(journey, segment, split_visas)
        evidence_payload = {
            "journey": journey,
            "segment": segment,
            "status": verdict["status"],
            "party": verdict["party"],
            "reason": verdict["reason"],
        }
        for key in ("interpretations", "legal_offsets", "seal", "seals"):
            if key in verdict:
                evidence_payload[key] = verdict[key]
        decide(
            db,
            "cold_attribution",
            object_id,
            verdict["status"],
            verdict["reason"],
            evidence_payload,
        )
        verdicts.append(verdict)
        statuses.append(verdict["status"])

    # A legacy whole-run object is superseded once a real reset visa divides
    # the run: the old certificate stays in history, never pretending the
    # earlier signing did not happen.
    legacy = db.execute(
        "SELECT * FROM decision WHERE system='cold_attribution' AND object_id=? "
        "ORDER BY id DESC LIMIT 1",
        (journey,),
    ).fetchone()
    if split_visas and legacy is not None:
        decide(
            db,
            "cold_attribution",
            journey,
            "superseded",
            "reset visa split the run; whole-run calibration cannot cross generations",
            {
                "journey": journey,
                "reset_visas": evidence["reset_visas"],
                "segments": evidence["segments"],
            },
        )
        statuses.append("superseded")

    if any(s == "evidence_conflict" for s in statuses):
        overall = "evidence_conflict"
    elif any(s == "attributed" for s in statuses):
        overall = "attributed"
    elif any(s == "alarm_unattributed" for s in statuses):
        overall = "alarm_unattributed"
    elif split_visas:
        overall = "superseded"
    else:
        overall = "no_evidence"
    return {"journey": journey, "status": overall, "verdicts": verdicts}


# ---------------------------------------------------------------------------
# One-budget intake recommendation — evidence coverage, not recency
# ---------------------------------------------------------------------------

def recommend_work_order(db, journey, obtainable):
    """Advise which single intake work order to open next.

    ``obtainable`` names the real witness sets the site can actually produce:
    ``anchor_region`` (a full-run dual time anchor pair) and/or
    ``seal_range`` (independently maintained temperature seals covering the
    same ranges).  The nearest original is never picked mechanically.
    """
    evidence = collect_evidence(db, journey)
    split_visas = evidence["has_reset_visa"]
    anchor_available = bool(obtainable.get("anchor_region"))
    seal_available = bool(obtainable.get("seal_range"))

    for segment in evidence["segments"]:
        recorder = _recorder_for_segment(evidence["recorders"], segment)
        if recorder is None:
            continue
        legal = _legal_offsets(
            recorder, evidence["anchors"], segment, split_visas
        )
        views = _interpretations(recorder, legal) if legal else {}
        trigger = recorder["trigger_minutes"]
        first_party = recorder["custody_window"]["first"]
        second_party = recorder["custody_window"]["second"]

        def all_hit(party):
            key = "first_minutes" if party == first_party else "second_minutes"
            return bool(views) and all(v[key] >= trigger for v in views.values())

        def any_hit(party):
            key = "first_minutes" if party == first_party else "second_minutes"
            return bool(views) and any(v[key] >= trigger for v in views.values())

        # Anchors help only when some legal offset draws a liability position
        # that the surviving ambiguity currently blocks.
        anchor_resolves_ambiguity = (
            any_hit(first_party) or any_hit(second_party)
        ) and not (all_hit(first_party) or all_hit(second_party))
        # If no offset reaches any threshold at all, clock calibration can
        # never produce a certificate; only physical seals can.
        no_offset_certifies = not (
            any_hit(first_party) or any_hit(second_party)
        )

        seals = _valid_seals(evidence["seals"], segment)
        covered_parties = {s["party"] for s in seals}
        both_parties = {first_party, second_party}
        missing_party_seal = bool(both_parties - covered_parties)

        if seal_available and (no_offset_certifies or missing_party_seal) and not anchor_resolves_ambiguity:
            return {
                "journey": journey,
                "segment": segment,
                "recommendation": "cold-seal-import",
                "reason": "no common offset can draw a liability position for the whole segment; same-range seals discriminate the parties directly",
            }
        if anchor_available and anchor_resolves_ambiguity:
            return {
                "journey": journey,
                "segment": segment,
                "recommendation": "cold-anchor-import",
                "reason": "two surviving interpretation groups can only be separated by the common clock offset; dual anchors cover the whole run",
            }
        if seal_available and not anchor_available:
            return {
                "journey": journey,
                "segment": segment,
                "recommendation": "cold-seal-import",
                "reason": "anchor region cannot be obtained; seals are the only admissible witness",
            }
        if anchor_available and not seal_available:
            return {
                "journey": journey,
                "segment": segment,
                "recommendation": "cold-anchor-import",
                "reason": "seal scans cannot be obtained; clock anchors are the only admissible witness",
            }
    return {
        "journey": journey,
        "recommendation": "none",
        "reason": "no obtainable witness set can resolve the surviving interpretations",
    }
