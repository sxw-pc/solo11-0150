"""Custody query helpers over the ledger recomputed by :mod:`marketproof.trade`.

The authoritative per-holder balances are rebuilt deterministically in
``trade.reconcile_all`` and persisted in ``custody_position``; this module only
reads them.  It never guesses a source: a quantity with several predecessors but
no per-unit destination stays pending in the trade ledger itself.
"""
from . import parties

DIRECT='direct'
TRANSFER='transfer'

def holder_balances(db,lot_id=None,include_empty=True):
    sql='SELECT lot_id,holder,SUM(quantity) AS quantity FROM custody_position'
    args=()
    if lot_id is not None:sql+=' WHERE lot_id=?';args=(lot_id,)
    sql+=' GROUP BY lot_id,holder ORDER BY lot_id,holder'
    rows={(r['lot_id'],r['holder']):int(r['quantity'] or 0) for r in db.execute(sql,args)}
    return rows if include_empty else {k:v for k,v in rows.items() if v}

def holders(db,lot_id):
    return {holder:qty for (lot,holder),qty in holder_balances(db,lot_id).items() if qty}

def external_quantity(db,receipt_id,memo=None):
    """Quantity entering an external purchaser through *confirmed* deliveries."""
    if memo is None:memo={}
    if receipt_id in memo:return memo[receipt_id]
    r=db.execute('SELECT * FROM trade_receipt WHERE id=?',(receipt_id,)).fetchone()
    if not r or r['status']!='confirmed':
        memo[receipt_id]=0;return 0
    if parties.is_external(db,r['buyer'],r['day']):
        memo[receipt_id]=int(r['quantity']);return memo[receipt_id]
    children=db.execute('SELECT id FROM trade_receipt WHERE previous_receipt=? AND status="confirmed"',
                        (receipt_id,)).fetchall()
    total=sum(external_quantity(db,c['id'],memo) for c in children)
    memo[receipt_id]=min(total,int(r['quantity']))
    return memo[receipt_id]

def returned_quantity(db,receipt_id,memo=None):
    """Quantity whose chain ends back with an earlier holder instead of an outsider."""
    if memo is None:memo={}
    if receipt_id in memo:return memo[receipt_id]
    r=db.execute('SELECT * FROM trade_receipt WHERE id=?',(receipt_id,)).fetchone()
    if not r or r['status']!='confirmed':
        memo[receipt_id]=0;return 0
    if parties.is_external(db,r['buyer'],r['day']):
        memo[receipt_id]=0;return 0
    children=db.execute('SELECT * FROM trade_receipt WHERE previous_receipt=? AND status="confirmed"',
                        (receipt_id,)).fetchall()
    if not children:
        memo[receipt_id]=int(r['quantity']);return memo[receipt_id]
    total=sum(returned_quantity(db,c['id'],memo) for c in children)
    memo[receipt_id]=min(total,int(r['quantity']))
    return memo[receipt_id]
