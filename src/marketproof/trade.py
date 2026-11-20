"""Bilateral receipts, quoted contract, custodial handover chain and cancellation.

Legal delivery and market price evidence are deliberately separate.  A receipt
is confirmed in the legal ledger whenever, at that step, both parties signed,
the quote covered it, the grade covered it and the quantity could be proved to
be in the seller's hands *at that moment*.  True resales remain confirmed even
when the same kilograms later circulate back to an earlier holder.  Price
products (see :mod:`marketproof.index`) decide separately which of those
deliveries may contribute evidence.
"""
from .store import ingest,decide
from . import parties

INVALID={'cancelled','voided','bilateral_conflict','quote_dispute','grade_dispute',
         'invalid_quantity','identity_dispute','source_pending'}

def _insert_receipt(db,system,data,trade,require_previous=False):
    """Store one immutable bilateral receipt.  Returns its identity decision."""
    tid=trade['id']
    existing=db.execute('SELECT id FROM trade_receipt WHERE id=?',(tid,)).fetchone()
    if existing:
        decide(db,'trade-identity',tid,'duplicate','same trade ID is immutable',[data['source_id']])
        return {'trade':tid,'status':'duplicate'}
    buyer=trade['buyer_view'];seller=trade['seller_view']
    declared=('previous_receipt' in trade) or ('previous_receipts' in trade)
    previous=trade.get('previous_receipt')
    if previous is None and 'previous_receipts' in trade:previous=trade.get('previous_receipts')
    previous_value=None
    if isinstance(previous,(list,tuple)):
        # Several sources without a per-unit destination must stay pending; we
        # never guess one predecessor just to make the chain balance.
        previous_value='['+','.join(str(x) for x in previous)+']'
        ambiguous=bool(previous)
    elif previous:
        previous_value=str(previous);ambiguous=False
    else:
        previous_value=None;ambiguous=False
    handover=trade.get('handover_at')
    identity=bool(trade.get('buyer') and trade.get('seller') and trade['buyer']!=trade['seller']
                  and trade.get('buyer_digest') and trade.get('seller_digest'))
    keys=['lot_id','quantity','day','price']
    if handover is not None:keys.append('handover_at')
    same=all(buyer.get(k)==seller.get(k)==trade.get(k) for k in keys)
    bilateral=identity and same and not ambiguous
    db.execute('INSERT INTO trade_receipt(id,quote_id,lot_id,stall,product,grade,day,price,quantity,buyer,seller,'
               'buyer_digest,seller_digest,bilateral_ok,settled,cancelled,status,previous_receipt,handover_at,source_system,previous_declared) '
               'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (tid,trade['quote_id'],trade['lot_id'],trade['stall'],trade['product'],trade['grade'],
                int(trade['day']),int(trade['price']),int(trade['quantity']),trade['buyer'],trade['seller'],
                trade['buyer_digest'],trade['seller_digest'],int(bilateral),int(bool(trade.get('settled'))),0,
                'source_pending' if ambiguous else 'pending',previous_value,
                int(handover) if handover is not None else None,system,int(declared)))
    if ambiguous:
        decide(db,'bilateral',tid,'pending','multiple source receipts without per-unit destination',[data['source_id']])
        decide(db,'trade-source',tid,'source_pending','several predecessors cannot name a single source',[data['source_id']])
        return {'trade':tid,'bilateral':'pending','source':'pending'}
    decide(db,'bilateral',tid,'consistent' if bilateral else 'conflict',
           'two independent receipts must agree on lot, quantity, day, price and handover time',[data['source_id']])
    if require_previous and not declared:
        decide(db,'trade-source',tid,'source_pending','a re-sale receipt must name exactly one predecessor',[data['source_id']])
        db.execute('UPDATE trade_receipt SET status="source_pending" WHERE id=?',(tid,))
        return {'trade':tid,'bilateral':'consistent' if bilateral else 'conflict','source':'pending'}
    if previous and not db.execute('SELECT id FROM trade_receipt WHERE id=?',(str(previous),)).fetchone():
        decide(db,'trade-source',tid,'source_pending','named predecessor receipt is not on file',[str(previous)])
        db.execute('UPDATE trade_receipt SET status="source_pending" WHERE id=?',(tid,))
        return {'trade':tid,'bilateral':'consistent' if bilateral else 'conflict','source':'pending'}
    if previous:
        parent=db.execute('SELECT buyer,lot_id,product,grade FROM trade_receipt WHERE id=?',(str(previous),)).fetchone()
        chain_ok=bool(parent and parent['buyer']==trade['seller'] and parent['lot_id']==trade['lot_id']
                      and parent['product']==trade['product'] and parent['grade']==trade['grade'])
        decide(db,'handover-chain',tid,'consistent' if chain_ok else 'conflict',
               'the named predecessor must have handed this lot to this seller',[str(previous)])
    return {'trade':tid,'bilateral':'consistent' if bilateral else 'conflict'}

def run(db,data):
    state=ingest(db,'trade',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    out=[]
    for trade in data.get('trades',[]):
        if trade.get('previous_receipt') and trade.get('evidence_type')!='physical_handover':
            # A re-sale in the legacy single-sale source is ambiguous evidence.
            decide(db,'handover-type',trade['id'],'unsupported',
                   'a re-sale receipt must state physical_handover evidence',[data['source_id']])
            out.append({'trade':trade['id'],'status':'unsupported'});continue
        out.append(_insert_receipt(db,'trade',data,trade))
    for tid in data.get('cancel',[]):
        db.execute('UPDATE trade_receipt SET cancelled=1 WHERE id=?',(tid,))
        decide(db,'cancellation',tid,'cancelled','cancel receipt releases occupied lot quantity',[data['source_id']])
    reconcile_all(db)
    return {'trades':out,'cancelled':data.get('cancel',[])}

def _quote_ok(db,t):
    quote=db.execute('SELECT * FROM quote_revision WHERE id=?',(t['quote_id'],)).fetchone()
    return bool(quote and quote['status']=='valid' and quote['stall']==t['stall'] and quote['product']==t['product']
                and quote['day']==t['day'] and quote['price']==t['price'])

def _grade_ok(db,t):
    grade=db.execute('SELECT * FROM grade_decision WHERE lot_id=?',(t['lot_id'],)).fetchone()
    lot=db.execute('SELECT * FROM lot_origin WHERE id=?',(t['lot_id'],)).fetchone()
    return bool(grade and grade['status']=='saleable' and grade['grade']==t['grade'] and lot and lot['product']==t['product']
                and t['day']<=grade['expires_day'])

def _identity_ok(db,party,day,stall=None,digest=None,strict=False):
    """Verify a party against a signed registration effective on the handover day.

    ``strict`` is used for handover receipts: an unregistered party cannot be
    trusted to be an independent operator.  Legacy single-sale receipts keep the
    original semantics when no party register exists for them.
    """
    regs=db.execute('SELECT * FROM party_register WHERE party=?',(party,)).fetchall()
    if not regs:return (False,None) if strict else (True,None)
    if digest:
        reg=parties.effective(db,party,day,digest=digest,stall=stall)
        return bool(reg),reg
    reg=parties.effective(db,party,day,stall=stall)
    return bool(reg),reg

def _base_status(db,t):
    if t['cancelled']:return 'cancelled'
    if db.execute('SELECT 1 FROM transfer_void WHERE receipt_id=? AND kind="physical_void" '
                  'AND status="effective"',(t['id'],)).fetchone():
        return 'voided'
    previous=t['previous_receipt']
    if previous and previous.startswith('['):return 'source_pending'
    if not t['previous_declared'] and (t['source_system']=='handover'
            or db.execute('SELECT COUNT(*) FROM trade_receipt WHERE source_system="handover" AND lot_id=?',(t['lot_id'],)).fetchone()[0]):
        return 'source_pending'
    if previous and not db.execute('SELECT id FROM trade_receipt WHERE id=?',(previous,)).fetchone():
        return 'source_pending'
    if not t['bilateral_ok']:return 'bilateral_conflict'
    chain=t['source_system']=='handover'
    seller_ok,_=_identity_ok(db,t['seller'],t['day'],t['stall'],t['seller_digest'],strict=chain)
    buyer_ok,_=_identity_ok(db,t['buyer'],t['day'],None,t['buyer_digest'],strict=chain)
    if not (seller_ok and buyer_ok):return 'identity_dispute'
    if not _quote_ok(db,t):return 'historical_dispute' if t['settled'] else 'quote_dispute'
    if not _grade_ok(db,t):return 'historical_dispute' if t['settled'] else 'grade_dispute'
    if int(t['quantity'])<=0:return 'invalid_quantity'
    return 'provisional'

def _order(t):
    at=t['handover_at'] if t['handover_at'] is not None else int(t['day'])*1000000
    return (int(at),int(t['day']),t['id'])

def effective_quantity(db,t):
    """Signed late corrections revise the quantity without overwriting the original.

    The receipt row keeps its original quantity as the immutable source; the
    custody chain uses the latest effective correction so upstream balances are
    restored and downstream deliveries that lose their source turn disputed.
    """
    row=db.execute('SELECT corrected_quantity FROM transfer_void WHERE receipt_id=? AND kind="correction" '
                   'AND status="effective" AND corrected_quantity IS NOT NULL ORDER BY effective_at DESC,id DESC LIMIT 1',
                   (t['id'],)).fetchone()
    return int(row['corrected_quantity']) if row else int(t['quantity'])



def _simulate(db,rows,statuses):
    """Resolve custody by handover time and predecessor link.

    Direct receipts (no predecessor, the legacy single-sale input) draw from the
    lot's ``grade_decision.saleable`` pool.  Re-sale receipts draw from the
    named predecessor's remaining custody, so a split can never exceed the
    holder's balance and the same kilogram cannot sit in two stalls.  Receipts
    that pass every check and have provable custody become ``confirmed``.
    """
    grades={r['lot_id']:r for r in db.execute('SELECT * FROM grade_decision')}
    positions={}          # receipt id -> remaining custody opened by that receipt
    open_positions=[]
    direct_used={}
    seeded=set()           # lots whose original saleable pool has a first direct sale
    processed=set()
    pending=sorted(rows,key=_order)

    def open_position(t,quantity,source):
        at=t['handover_at'] if t['handover_at'] is not None else int(t['day'])
        open_positions.append({'lot_id':t['lot_id'],'holder':t['buyer'],'receipt_id':t['id'],
                               'opened_at':int(at),'quantity':int(quantity),'source':source})
        positions[t['id']]=len(open_positions)-1

    def settle_unseeded():
        # Any delivery still unplaced because its declared predecessor never
        # settled also loses its source.
        for t in pending:
            if t['id'] not in processed:
                statuses[t['id']]='custody_dispute'
                processed.add(t['id'])

    while pending:
        progress=False;waiting=[]
        for t in pending:
            status=statuses[t['id']]
            if status in INVALID:
                processed.add(t['id']);progress=True;continue
            parent=t['previous_receipt']
            if parent and parent not in processed:
                waiting.append(t);continue
            qty=effective_quantity(db,t)
            if parent:
                if statuses.get(parent) not in ('confirmed','historical_dispute'):
                    statuses[t['id']]='custody_dispute'
                else:
                    idx=positions.get(parent)
                    if idx is None:
                        statuses[t['id']]='custody_dispute'
                    else:
                        available=open_positions[idx]['quantity']
                        if qty<=0:
                            statuses[t['id']]='invalid_quantity'
                        elif qty>available:
                            statuses[t['id']]='overdraw'
                        else:
                            open_positions[idx]['quantity']-=qty
                            open_position(t,qty,'transfer')
                            if status=='provisional':statuses[t['id']]='confirmed'
            else:
                if status=='provisional':
                    if t['source_system']=='handover' and t['lot_id'] in seeded:
                        # A handover source may only start the chain once; any
                        # later sale must name the exact previous receipt.
                        statuses[t['id']]='source_pending'
                    else:
                        grade=grades.get(t['lot_id'])
                        used=direct_used.get(t['lot_id'],0)
                        saleable=int(grade['saleable']) if grade else 0
                        if qty<=0:
                            statuses[t['id']]='invalid_quantity'
                        elif used+qty>saleable:
                            statuses[t['id']]='overdraw'
                        else:
                            direct_used[t['lot_id']]=used+qty
                            open_position(t,qty,'direct')
                            statuses[t['id']]='confirmed'
                    if statuses[t['id']]=='confirmed':seeded.add(t['lot_id'])
                elif status=='historical_dispute':
                    # Settled delivery keeps its historical occupation.
                    used=direct_used.get(t['lot_id'],0)
                    direct_used[t['lot_id']]=used+qty
                    open_position(t,qty,'direct')
            processed.add(t['id']);progress=True
        if not progress:
            # A predecessor cycle or missing predecessor: hold it for review.
            for t in waiting:
                statuses[t['id']]='custody_dispute';processed.add(t['id'])
            break
        pending=waiting
    settle_unseeded()
    return direct_used,open_positions

def reconcile_all(db):
    rows=db.execute('SELECT * FROM trade_receipt ORDER BY day,id').fetchall()
    statuses={t['id']:_base_status(db,t) for t in rows}
    direct_used,positions=_simulate(db,rows,statuses)
    for t in rows:
        status=statuses[t['id']]
        reason='licence quote, grade, bilateral receipt and custody balance'
        if status=='overdraw':reason='split transfers exceed the holder balance at that moment'
        elif status=='custody_dispute':reason='no provable custody balance or the predecessor handover was voided'
        elif status=='voided':reason='signed evidence proves the physical handover never happened'
        elif status=='source_pending':reason='several source receipts without per-unit destination'
        elif status=='identity_dispute':reason='no signed party registration effective at handover'
        old=db.execute('SELECT status,version FROM trade_decision WHERE trade_id=?',(t['id'],)).fetchone()
        version=old['version'] if old and old['status']==status else old['version']+1 if old else 1
        db.execute('INSERT INTO trade_decision(trade_id,status,reason,version) VALUES(?,?,?,?) '
                   'ON CONFLICT(trade_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,version=excluded.version',
                   (t['id'],status,reason,version))
        db.execute('UPDATE trade_receipt SET status=? WHERE id=?',(status,t['id']))
        evidence=[t['quote_id'],t['lot_id']]
        if t['previous_receipt']:evidence.append(t['previous_receipt'])
        decide(db,'trade',t['id'],status,reason,evidence)
    for lot in db.execute('SELECT id FROM lot_origin').fetchall():
        grade=db.execute('SELECT saleable FROM grade_decision WHERE lot_id=?',(lot['id'],)).fetchone()
        old=db.execute('SELECT * FROM lot_balance WHERE lot_id=?',(lot['id'],)).fetchone()
        saleable=grade['saleable'] if grade else 0;used=direct_used.get(lot['id'],0)
        version=old['version'] if old and (old['saleable'],old['occupied'])==(saleable,used) else old['version']+1 if old else 1
        db.execute('INSERT INTO lot_balance(lot_id,saleable,occupied,version) VALUES(?,?,?,?) '
                   'ON CONFLICT(lot_id) DO UPDATE SET saleable=excluded.saleable,occupied=excluded.occupied,version=excluded.version',
                   (lot['id'],saleable,used,version))
    db.execute('DELETE FROM custody_position')
    for p in positions:
        db.execute('INSERT INTO custody_position(lot_id,holder,receipt_id,opened_at,quantity,source) VALUES(?,?,?,?,?,?)',
                   (p['lot_id'],p['holder'],p['receipt_id'],p['opened_at'],p['quantity'],p['source']))
    from .index import recompute_all
    recompute_all(db)
