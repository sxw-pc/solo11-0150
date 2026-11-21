"""Offline price-slip capture.

Two shapes share this entry:

* legacy integer slips (no ``capture`` key): ``quote.displayed_price`` is
  converted to an integer and handed to the quote ledger unchanged;
* evidence captures (``capture`` key): signed originals clipped to one
  number string (batch) are solved into a single unit-price basis before
  any requested quote is issued.  Requests whose originals do not fix one
  price stay held for verification, and the response names the one real
  paper slip or co-signed trial basket that settles the remaining
  readings.

A capture covers one stall, one quote day and at most three unknown base
prices.  Two kinds of signed originals count as evidence:

* ``marked_rows`` — the re-captured same-version price sheet: product,
  base quantity and displayed price, with provable row number and basis
  (row, sheet, sheet_version);
* ``trial_tickets`` — a publicly trial-bought combination: quantities
  co-signed by scale and counter plus the complete rounded total.  The
  batch convention fixes how the total was rounded (``per_line`` keeps
  the cumulative per-line difference and the final change, ``total``
  rounds once at the end) and whether the basket deposit is included.

A settlement total buried in a ticket is never read as a price-list row;
it only constrains the shared basis through the batch convention.
"""

import itertools
import json
from fractions import Fraction

from . import quote
from .store import decide, ingest

CONVENTIONS = ("per_line", "total")
MAX_PRODUCTS = 3
MAX_UNIT = 10000
ENUM_LIMIT = 2000000

DDL = """
CREATE TABLE IF NOT EXISTS slip_batch(
    id TEXT PRIMARY KEY, stall TEXT, day INTEGER, convention TEXT,
    deposit INTEGER, status TEXT, candidates TEXT, version INTEGER);
CREATE TABLE IF NOT EXISTS slip_original(
    id TEXT PRIMARY KEY, batch_id TEXT, kind TEXT, payload TEXT, source_id TEXT);
CREATE TABLE IF NOT EXISTS slip_issue(
    quote_id TEXT, batch_id TEXT, product TEXT, price INTEGER, source_id TEXT,
    PRIMARY KEY(quote_id, batch_id));
CREATE TABLE IF NOT EXISTS slip_recommendation(
    id INTEGER PRIMARY KEY, batch_id TEXT, source_id TEXT, kind TEXT,
    payload TEXT, status TEXT);
"""


def ensure(db):
    db.executescript(DDL)


def run(db, data):
    status = ingest(db, "price_slip", data)
    if status != "new":
        return {"status": status}
    if "capture" not in data:
        raw = data["quote"]
        price = int(raw["displayed_price"])
        item = {"price": price, **raw["entry"]}
        return quote.run(db, {"source_id": data["source_id"] + "-quote", "quotes": [item]})
    ensure(db)
    return run_capture(db, data)


def review(db):
    """Current capture ledger: batches, originals, issues and proposals."""
    ensure(db)
    out = {}
    for name in ("slip_batch", "slip_original", "slip_issue", "slip_recommendation"):
        out[name] = [dict(r) for r in db.execute(f"SELECT * FROM {name} ORDER BY 1")]
    out["held_quotes"] = [dict(r) for r in db.execute(
        "SELECT * FROM quote_revision WHERE status='held' ORDER BY id")]
    return out


# ---------- validation ----------

def validate_capture(cap):
    if not isinstance(cap, dict):
        return "capture must be an object"
    if not cap.get("stall") or "day" not in cap:
        return "one capture covers one stall and one quote day"
    spec = cap.get("batch")
    if not isinstance(spec, dict) or not spec.get("id"):
        return "capture batch needs the number string id"
    if spec.get("convention") not in CONVENTIONS:
        return "batch convention must be one of the two pre-signed rounding conventions"
    deposit = spec.get("deposit", 0)
    if not isinstance(deposit, int) or isinstance(deposit, bool) or deposit < 0:
        return "basket deposit must be a non-negative integer"
    products = cap.get("products")
    if (not isinstance(products, list) or not 1 <= len(products) <= MAX_PRODUCTS
            or len(set(products)) != len(products)):
        return "one capture carries one to three unknown base prices"
    for entry in cap.get("requests", []):
        missing = [k for k in ("id", "licence_id", "stall", "region", "product",
                               "day", "version", "digest") if k not in entry]
        if missing:
            return "request %r misses %s" % (entry.get("id"), ",".join(missing))
    return None


def validate_original(kind, raw, products):
    if not isinstance(raw, dict):
        return "original must be an object"
    if not raw.get("id"):
        return "original needs an id"
    if raw.get("signed") is not True:
        return "an unsigned combination cannot impersonate a signed original"
    if kind == "marked":
        if raw.get("product") not in products:
            return "marked row names a product outside this number string"
        if any(raw.get(k) in (None, "") for k in ("row", "sheet", "sheet_version")):
            return "marked row needs a provable row number and basis: row, sheet, sheet_version"
        qty, price = raw.get("quantity"), raw.get("price")
        if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0:
            return "marked row quantity must be positive"
        if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
            return "marked row price must be a positive integer"
        return None
    quantities = raw.get("quantities")
    if not isinstance(quantities, dict) or not quantities:
        return "trial ticket needs the co-signed combination quantities"
    for product, qty in quantities.items():
        if product not in products:
            return "trial ticket names a product outside this number string"
        if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0:
            return "trial quantities must be positive"
    total = raw.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        return "trial ticket needs the complete signed total"
    return None


def parse_original(kind, raw):
    if kind == "marked":
        return {"id": raw["id"], "kind": "marked", "product": raw["product"],
                "quantity": Fraction(str(raw["quantity"])), "price": int(raw["price"]),
                "row": raw["row"], "sheet": raw["sheet"],
                "sheet_version": int(raw["sheet_version"])}
    return {"id": raw["id"], "kind": "trial",
            "quantities": {p: Fraction(str(q)) for p, q in raw["quantities"].items()},
            "total": int(raw["total"])}


# ---------- originals ledger ----------

def add_originals(db, batch_id, sid, cap, products):
    added, rejected = [], []
    items = ([("marked", o) for o in cap.get("marked_rows") or []]
             + [("trial", o) for o in cap.get("trial_tickets") or []])
    for kind, raw in items:
        problem = validate_original(kind, raw, products)
        oid = raw.get("id") if isinstance(raw, dict) else None
        if problem:
            rejected.append(oid)
            decide(db, "original", str(oid), "rejected", problem, [sid])
            continue
        payload = json.dumps({"kind": kind, **raw}, sort_keys=True, ensure_ascii=False)
        cur = db.execute(
            "INSERT OR IGNORE INTO slip_original(id,batch_id,kind,payload,source_id) VALUES(?,?,?,?,?)",
            (oid, batch_id, kind, payload, sid))
        if cur.rowcount == 0:
            old = db.execute("SELECT payload FROM slip_original WHERE id=?", (oid,)).fetchone()
            if old["payload"] != payload:
                rejected.append(oid)
                decide(db, "original-identity", oid, "conflict",
                       "same original ID changed bytes", [sid])
            continue
        added.append(oid)
    return added, rejected


def load_originals(db, batch_id):
    out = []
    for r in db.execute("SELECT kind, payload FROM slip_original WHERE batch_id=? ORDER BY id",
                        (batch_id,)):
        out.append(parse_original(r["kind"], json.loads(r["payload"])))
    return out


# ---------- price basis solver ----------

def round_half_up(amount):
    """Round a non-negative Fraction to the nearest integer, halves up."""
    return (2 * amount.numerator + amount.denominator) // (2 * amount.denominator)


def expected_total(convention, deposit, lines):
    """Signed total a batch convention produces for (quantity, unit) lines.

    ``per_line`` rounds every line on its own and keeps the cumulative
    difference — the final change is part of the sum; ``total`` rounds the
    exact sum once.  The basket deposit is added after rounding.
    """
    if convention == "per_line":
        return sum(round_half_up(q * p) for q, p in lines) + deposit
    return round_half_up(sum((q * p for q, p in lines), Fraction(0))) + deposit


def scope_of(original):
    if original["kind"] == "marked":
        return {original["product"]}
    return set(original["quantities"])


def matches(original, candidate, convention, deposit):
    if original["kind"] == "marked":
        return original["quantity"] * candidate[original["product"]] == original["price"]
    lines = [(q, candidate[p]) for p, q in original["quantities"].items()]
    return expected_total(convention, deposit, lines) == original["total"]


def solve(products, originals, convention, deposit):
    """All positive-integer unit-price vectors explaining every original.

    Returns (candidates, unconstrained) or None when the evidence space is
    too large to enumerate today.
    """
    constrained = [p for p in products if any(p in scope_of(o) for o in originals)]
    unconstrained = [p for p in products if p not in constrained]
    bounds = {}
    for p in constrained:
        bound = MAX_UNIT
        for o in originals:
            if o["kind"] == "marked" and o["product"] == p:
                bound = min(bound, int(o["price"] / o["quantity"]) + 1)
            elif o["kind"] == "trial" and o["quantities"].get(p):
                bound = min(bound, int(o["total"] / o["quantities"][p]) + 2 + deposit)
        bounds[p] = max(1, bound)
    space = 1
    for p in constrained:
        space *= bounds[p]
    if space > ENUM_LIMIT:
        return None
    candidates = []
    for vec in itertools.product(*(range(1, bounds[p] + 1) for p in constrained)):
        candidate = dict(zip(constrained, vec))
        if all(matches(o, candidate, convention, deposit) for o in originals):
            candidates.append(candidate)
    return candidates, unconstrained


def provable_prices(products, candidates, unconstrained):
    """A row is provable iff every surviving reading fixes the same price."""
    prices = {}
    for p in products:
        if p in unconstrained or not candidates:
            prices[p] = None
            continue
        values = {c[p] for c in candidates}
        prices[p] = values.pop() if len(values) == 1 else None
    return prices


def sorted_candidates(candidates, products):
    return sorted(({p: c[p] for p in products if p in c} for c in candidates),
                  key=lambda c: tuple(c.get(p, 0) for p in products))


# ---------- batch state ----------

def set_batch_status(db, batch, status, reason, evidence):
    if batch["status"] == status:
        return
    db.execute("UPDATE slip_batch SET status=?, version=version+1 WHERE id=?",
               (status, batch["id"]))
    decide(db, "basis", batch["id"], status, reason, evidence)


def store_candidates(db, batch, candidates):
    blob = json.dumps(candidates, sort_keys=True)
    if blob != batch["candidates"]:
        db.execute("UPDATE slip_batch SET candidates=?, version=version+1 WHERE id=?",
                   (blob, batch["id"]))


# ---------- capture flow ----------

def run_capture(db, data):
    sid = data["source_id"]
    cap = data["capture"]
    problem = validate_capture(cap)
    if problem:
        decide(db, "price-basis", sid, "rejected", problem, [])
        return {"source_id": sid, "status": "rejected", "reason": problem}
    stall, day, products = cap["stall"], int(cap["day"]), list(cap["products"])
    spec = cap["batch"]
    batch_id, convention = spec["id"], spec["convention"]
    deposit = int(spec.get("deposit", 0))
    batch = db.execute("SELECT * FROM slip_batch WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        db.execute("INSERT INTO slip_batch(id,stall,day,convention,deposit,status,candidates,version)"
                   " VALUES(?,?,?,?,?,'open','[]',1)", (batch_id, stall, day, convention, deposit))
        decide(db, "basis", batch_id, "open",
               "one stall, one quote day, one pre-signed rounding convention", [sid])
        batch = db.execute("SELECT * FROM slip_batch WHERE id=?", (batch_id,)).fetchone()
    elif (batch["stall"], batch["day"], batch["convention"], batch["deposit"]) != (stall, day, convention, deposit):
        set_batch_status(db, batch, "mixed",
                         "same number string redeclared with a different stall, day, "
                         "rounding convention or deposit", [sid])
        batch = db.execute("SELECT * FROM slip_batch WHERE id=?", (batch_id,)).fetchone()
        return finish(db, sid, cap, batch, [], [])
    added, rejected = add_originals(db, batch_id, sid, cap, products)
    if batch["status"] != "open":
        return finish(db, sid, cap, batch, added, rejected)
    originals = load_originals(db, batch_id)
    solved = solve(products, originals, convention, deposit)
    if solved is None:
        return finish(db, sid, cap, batch, added, rejected,
                      note="evidence space too large to enumerate today")
    candidates, unconstrained = solved
    if not candidates:
        set_batch_status(db, batch, "contradicted",
                         "no single unit-price basis explains every signed original "
                         "on this number string", [o["id"] for o in originals])
        batch = db.execute("SELECT * FROM slip_batch WHERE id=?", (batch_id,)).fetchone()
    else:
        store_candidates(db, batch, candidates)
        batch = db.execute("SELECT * FROM slip_batch WHERE id=?", (batch_id,)).fetchone()
    return finish(db, sid, cap, batch, added, rejected,
                  candidates=candidates, unconstrained=unconstrained, originals=originals)


def finish(db, sid, cap, batch, added, rejected,
           candidates=None, unconstrained=None, originals=None, note=None):
    products = list(cap["products"])
    originals = load_originals(db, batch["id"]) if originals is None else originals
    candidates = [] if candidates is None else candidates
    unconstrained = products if unconstrained is None else unconstrained
    prices = provable_prices(products, candidates, unconstrained)
    issued, held, blocked = issue_requests(db, sid, cap, batch, prices, originals)
    missing = [{"product": p, "needs": "a signed marked row or one separating trial combination"}
               for p in products if prices[p] is None]
    recommendation = recommend(db, sid, cap, batch, products, candidates,
                               held, blocked, issued)
    out = {"source_id": sid, "status": "captured", "batch": batch["id"],
           "batch_status": batch["status"], "convention": batch["convention"],
           "products": products, "candidates": sorted_candidates(candidates, products),
           "candidate_count": len(candidates), "unit_prices": prices,
           "issued": issued, "held": held, "version_blocked": blocked,
           "missing": missing, "added_originals": added, "rejected_originals": rejected,
           "recommendation": recommendation}
    if note:
        out["note"] = note
    return out


def version_clash(db, entry, price):
    rows = db.execute(
        'SELECT id, price FROM quote_revision WHERE status!="held" AND '
        "(id=? OR (stall=? AND product=? AND day=? AND version=?))",
        (entry["id"], entry["stall"], entry["product"],
         int(entry["day"]), int(entry["version"]))).fetchall()
    for r in rows:
        if r["price"] != price:
            return r
    return None


def issue_requests(db, sid, cap, batch, prices, originals):
    issued, held, blocked = [], [], []
    evidence_ids = [o["id"] for o in originals] + [sid]
    provable = []
    for entry in cap.get("requests", []):
        qid, product = entry["id"], entry["product"]
        if (entry.get("stall") != cap["stall"] or int(entry.get("day", -1)) != int(cap["day"])
                or product not in cap["products"]):
            reason = "request lies outside the captured stall, day or number string"
            quote.hold(db, entry, reason, [sid])
            held.append({"quote": qid, "product": product, "status": "held",
                         "code": "scope", "reason": reason})
            continue
        if batch["status"] != "open":
            reason = ("batch basis " + batch["status"] + "; issued vouchers are retained "
                      "as certified and no new issuance uses this basis")
            quote.hold(db, entry, reason, evidence_ids)
            held.append({"quote": qid, "product": product, "status": "held",
                         "code": "batch", "reason": reason})
            continue
        price = prices.get(product)
        if price is None:
            reason = "insufficient signed originals: no single unit price is fixed for this product"
            quote.hold(db, entry, reason, evidence_ids)
            held.append({"quote": qid, "product": product, "status": "held",
                         "code": "insufficient", "reason": reason})
            continue
        clash = version_clash(db, entry, price)
        if clash is not None:
            decide(db, "quote", qid, "version_blocked",
                   "existing quote version cannot be split; request a fresh version", [sid])
            blocked.append({"quote": qid, "product": product, "derived_price": price,
                            "existing_quote": clash["id"], "existing_price": clash["price"],
                            "reason": "existing quote version cannot be split"})
            continue
        provable.append((entry, price))
    if provable:
        result = quote.run(db, {"source_id": sid + "-quotes",
                                "quotes": [{"price": p, **e} for e, p in provable]})
        for (entry, price), outcome in zip(provable, result.get("quotes", [])):
            issued.append({"quote": outcome["quote"], "product": entry["product"],
                           "status": outcome["status"], "price": price})
            db.execute("INSERT OR IGNORE INTO slip_issue(quote_id,batch_id,product,price,source_id)"
                       " VALUES(?,?,?,?,?)",
                       (entry["id"], batch["id"], entry["product"], price, sid))
    return issued, held, blocked


# ---------- continuation recommendation ----------

def separating_basket(products, candidates, convention, deposit, max_q=4):
    """One small basket whose single signed reading settles every candidate.

    Computed jointly from the still undecided originals — never a fixed
    recipe, never the basket that was already read.
    """
    constrained = [p for p in products if p in candidates[0]]
    for qs in itertools.product(range(1, max_q + 1), repeat=len(constrained)):
        totals = {}
        for candidate in candidates:
            total = expected_total(convention, deposit,
                                   [(Fraction(q), candidate[p])
                                    for p, q in zip(constrained, qs)])
            if total in totals.values():
                break
            totals[json.dumps(candidate, sort_keys=True)] = total
        else:
            expectations = [{"candidate": {p: c[p] for p in constrained},
                             "total": totals[json.dumps(c, sort_keys=True)]}
                            for c in candidates]
            return dict(zip(constrained, qs)), expectations
    return None


def recommend(db, sid, cap, batch, products, candidates, held, blocked, issued):
    availability = cap.get("availability") or {}
    conditions = {
        "available_marked_rows": list(availability.get("marked_rows") or []),
        "trial_scale": bool(availability.get("trial_scale")),
        "existing_versions": [dict(r) for r in db.execute(
            "SELECT id, product, version, price, status FROM quote_revision"
            " WHERE stall=? AND day=? AND status!='held' ORDER BY id",
            (cap["stall"], int(cap["day"])))],
    }
    rec = None
    if batch["status"] != "open":
        rec = {"kind": "none", "status": "proposed", "conditions": conditions,
               "reason": "basis of this number string is " + batch["status"] +
                        "; keep issued vouchers as certified and do not issue new ones"}
    else:
        needed = [h["product"] for h in held if h.get("code") == "insufficient"]
        if needed:
            for product in [p for p in products if p in needed]:
                row = next((r for r in conditions["available_marked_rows"]
                            if r.get("product") == product), None)
                if row:
                    rec = {"kind": "marked", "status": "proposed", "product": product,
                           "row": row.get("row"), "sheet": row.get("sheet"),
                           "sheet_version": row.get("sheet_version"),
                           "conditions": conditions,
                           "reason": "re-capture the same-version marked sheet with its "
                                     "edges and verify on the actually read row"}
                    break
            if rec is None and conditions["trial_scale"] and len(candidates) > 1:
                found = separating_basket(products, candidates,
                                          batch["convention"], batch["deposit"])
                if found is not None:
                    quantities, expectations = found
                    rec = {"kind": "trial", "status": "proposed", "quantities": quantities,
                           "expectations": expectations, "conditions": conditions,
                           "reason": "one co-signed trial basket settles every remaining "
                                     "reading; computed jointly with the undecided originals"}
            if rec is None:
                rec = {"kind": "none", "status": "proposed", "conditions": conditions,
                       "reason": "no single additional signed original can settle the "
                                 "requested rows today"}
        else:
            covered = {i["product"] for i in issued}
            uncovered = [b for b in blocked if b["product"] not in covered]
            if uncovered:
                rec = {"kind": "reissue", "status": "proposed",
                       "quotes": [b["quote"] for b in uncovered],
                       "conditions": conditions,
                       "reason": "derived price differs from a non-splittable existing "
                                 "quote version; re-request with a fresh version"}
    if rec is None:
        return None
    db.execute("INSERT INTO slip_recommendation(batch_id,source_id,kind,payload,status)"
               " VALUES(?,?,?,?,?)",
               (batch["id"], sid, rec["kind"],
                json.dumps(rec, sort_keys=True, ensure_ascii=False), rec["status"]))
    return rec
