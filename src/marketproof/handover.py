"""Offline handover capture: re-sales with splits, voids and late corrections.

Each receipt in a ``handovers`` source is one signed bilateral view naming the
lot, quantity, price, handover time, exactly one ``previous_receipt`` and the
transferee.  Splitting is expressed by several receipts that all name the same
predecessor.  ``voids`` distinguish the evidence type explicitly:

* ``physical_void`` -- a signed proof that the physical handover never happened;
  custody is restored upstream and downstream deliveries lose their source;
* ``refund`` / ``contract_cancel`` -- money moved but goods stayed put; custody
  does **not** move back, so a cancelled receipt cannot conjure goods at a stall
  that never physically held them;
* ``correction`` -- a late bilateral correction of the quantity of an earlier
  receipt, applied as a signed revision rather than an overwrite.

The legacy ``trade`` source keeps its original meaning: a single direct sale
whose ``cancel`` releases the lot's unoccupied quantity.  New re-sale receipts
are only accepted from this handover source (or with an explicit
``evidence_type``), so the two semantics are never mixed silently.
"""
from .store import ingest,decide
from .trade import _insert_receipt,reconcile_all

KINDS={'physical_void','refund','contract_cancel','correction'}

def run(db,data):
    state=ingest(db,'handover',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    out=[]
    for trade in data.get('handovers',[]):
        # A re-sale receipt must state its evidence kind; it is never inferred
        # from a bare receipt or from a cancellation flag.
        if trade.get('evidence_type')!='physical_handover':
            decide(db,'handover-type',trade['id'],'unsupported',
                   'a handover receipt must explicitly declare physical_handover evidence',[data['source_id']])
            out.append({'trade':trade['id'],'status':'unsupported'});continue
        out.append(_insert_receipt(db,'handover',data,trade,require_previous=True))
    voided=[]
    for void in data.get('voids',[]):
        voided.append(_apply_void(db,data,void))
    reconcile_all(db)
    return {'handovers':out,'voids':voided}

def _apply_void(db,data,void):
    vid=void['id'];kind=void.get('kind')
    if kind not in KINDS:
        decide(db,'void',vid,'unsupported','void kind must be physical_void, refund, contract_cancel or correction',[data['source_id']])
        return {'void':vid,'status':'unsupported'}
    target=void.get('receipt_id')
    if target and not db.execute('SELECT id FROM trade_receipt WHERE id=?',(target,)).fetchone():
        decide(db,'void',vid,'source_pending','void names a receipt that is not on file',[target])
        return {'void':vid,'status':'source_pending'}
    # Two-witness rule: a void needs two signed parties, not a bare flag.
    signed=bool(void.get('witnessed_by') and len(void['witnessed_by'])>=2 and void.get('evidence_digest'))
    status='effective' if signed else 'unwitnessed'
    existing=db.execute('SELECT * FROM transfer_void WHERE id=?',(vid,)).fetchone()
    if existing:
        decide(db,'void-identity',vid,'duplicate','same void ID is immutable',[data['source_id']])
        return {'void':vid,'status':'duplicate'}
    db.execute('INSERT INTO transfer_void(id,receipt_id,kind,evidence_digest,signed_by,corrected_quantity,effective_at,status,reason,source_ref) '
               'VALUES(?,?,?,?,?,?,?,?,?,?)',
               (vid,target,kind,void.get('evidence_digest'),
                ','.join(void.get('witnessed_by',[])),int(void['corrected_quantity']) if void.get('corrected_quantity') is not None else None,
                int(void.get('effective_at',data.get('business_time',0))),status,void.get('reason',''),data['source_id']))
    decide(db,'void',vid,status,f'{kind} evidence {"restores upstream custody" if kind=="physical_void" else "does not move goods"}',
           [target or '',void.get('evidence_digest','')])
    if kind=='correction' and status=='effective' and void.get('corrected_quantity') is not None:
        # The receipt row keeps its original quantity; the corrected value is
        # applied to custody at reconciliation time so the original source stays
        # on file and prior occupied history is not overwritten.
        decide(db,'trade-correction',target,'corrected',
               'late bilateral correction of quantity, original receipt retained',
               [vid,data['source_id'],int(void['corrected_quantity'])])
    return {'void':vid,'kind':kind,'status':status,'receipt':target}
