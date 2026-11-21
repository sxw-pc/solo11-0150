"""Bilateral receipts, contract price, conservation of lot balance and cancellation."""
from .store import ingest,decide

def run(db,data):
    state=ingest(db,'trade',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    out=[]
    for trade in data.get('trades',[]):
        tid=trade['id'];existing=db.execute('SELECT id FROM trade_receipt WHERE id=?',(tid,)).fetchone()
        if existing:
            decide(db,'trade-identity',tid,'duplicate','same trade ID is immutable',[data['source_id']]);continue
        buyer=trade['buyer_view'];seller=trade['seller_view']
        identity=bool(trade.get('buyer') and trade.get('seller') and trade['buyer']!=trade['seller']
                      and trade.get('buyer_digest') and trade.get('seller_digest'))
        same=all(buyer.get(k)==seller.get(k)==trade.get(k) for k in ('lot_id','quantity','day','price'))
        bilateral=identity and same
        db.execute('INSERT INTO trade_receipt(id,quote_id,lot_id,stall,product,grade,day,price,quantity,buyer,seller,buyer_digest,seller_digest,bilateral_ok,settled,cancelled,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (tid,trade['quote_id'],trade['lot_id'],trade['stall'],trade['product'],trade['grade'],int(trade['day']),int(trade['price']),int(trade['quantity']),trade['buyer'],trade['seller'],trade['buyer_digest'],trade['seller_digest'],int(bilateral),int(bool(trade.get('settled'))),0,'pending'))
        decide(db,'bilateral',tid,'consistent' if bilateral else 'conflict','two independent receipts must agree on lot, quantity, day and price',[data['source_id']])
        out.append({'trade':tid,'bilateral':'consistent' if bilateral else 'conflict'})
    for tid in data.get('cancel',[]):
        db.execute('UPDATE trade_receipt SET cancelled=1 WHERE id=?',(tid,))
        decide(db,'cancellation',tid,'cancelled','cancel receipt releases occupied lot quantity',[data['source_id']])
    reconcile_all(db)
    return {'trades':out,'cancelled':data.get('cancel',[])}

def reconcile_all(db):
    occupied={}
    rows=db.execute('SELECT * FROM trade_receipt ORDER BY day,id').fetchall()
    for t in rows:
        quote=db.execute('SELECT * FROM quote_revision WHERE id=?',(t['quote_id'],)).fetchone()
        grade=db.execute('SELECT * FROM grade_decision WHERE lot_id=?',(t['lot_id'],)).fetchone()
        lot=db.execute('SELECT * FROM lot_origin WHERE id=?',(t['lot_id'],)).fetchone()
        q_ok=bool(quote and quote['status']=='valid' and quote['stall']==t['stall'] and quote['product']==t['product']
                  and quote['day']==t['day'] and quote['price']==t['price'])
        g_ok=bool(grade and grade['status']=='saleable' and grade['grade']==t['grade'] and lot and lot['product']==t['product']
                  and t['day']<=grade['expires_day'])
        used=occupied.get(t['lot_id'],0)
        if t['cancelled']:status='cancelled'
        elif not t['bilateral_ok']:status='bilateral_conflict'
        elif not q_ok:status='historical_dispute' if t['settled'] else 'quote_dispute'
        elif not g_ok:status='historical_dispute' if t['settled'] else 'grade_dispute'
        elif int(t['quantity'])<=0:status='invalid_quantity'
        elif used+t['quantity']>grade['saleable']:status='overdraw'
        else:status='confirmed'
        if status=='confirmed' or (status=='historical_dispute' and t['settled']):
            occupied[t['lot_id']]=used+t['quantity']
        old=db.execute('SELECT status,version FROM trade_decision WHERE trade_id=?',(t['id'],)).fetchone()
        version=old['version'] if old and old['status']==status else old['version']+1 if old else 1
        db.execute('INSERT INTO trade_decision(trade_id,status,reason,version) VALUES(?,?,?,?) '
                   'ON CONFLICT(trade_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,version=excluded.version',
                   (t['id'],status,'licence quote, grade, bilateral receipt and lot conservation',version))
        db.execute('UPDATE trade_receipt SET status=? WHERE id=?',(status,t['id']))
        decide(db,'trade',t['id'],status,f'lot occupied {occupied.get(t["lot_id"],0)} after transaction',[t['quote_id'],t['lot_id']])
    for lot in db.execute('SELECT id FROM lot_origin').fetchall():
        grade=db.execute('SELECT saleable FROM grade_decision WHERE lot_id=?',(lot['id'],)).fetchone()
        old=db.execute('SELECT * FROM lot_balance WHERE lot_id=?',(lot['id'],)).fetchone()
        saleable=grade['saleable'] if grade else 0;used=occupied.get(lot['id'],0)
        version=old['version'] if old and (old['saleable'],old['occupied'])==(saleable,used) else old['version']+1 if old else 1
        db.execute('INSERT INTO lot_balance(lot_id,saleable,occupied,version) VALUES(?,?,?,?) '
                   'ON CONFLICT(lot_id) DO UPDATE SET saleable=excluded.saleable,occupied=excluded.occupied,version=excluded.version',
                   (lot['id'],saleable,used,version))
    from .index import recompute_all
    recompute_all(db)
