"""Stall licence scope, quote version conflicts and withdrawals."""
from .store import ingest,decide

def run(db,data):
    state=ingest(db,'quote',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    for lic in data.get('licences',[]):
        db.execute('INSERT INTO stall_licence(id,stall,region,product,starts,ends,status) VALUES(?,?,?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET status=excluded.status',
                   (lic['id'],lic['stall'],lic['region'],lic['product'],int(lic['starts']),int(lic['ends']),lic.get('status','active')))
    out=[]
    for quote in data.get('quotes',[]):
        qid=quote['id'];price=int(quote['price'])
        old=db.execute('SELECT * FROM quote_revision WHERE id=?',(qid,)).fetchone()
        if old:
            status='duplicate' if old['price']==price and old['digest']==quote['digest'] else 'conflict'
            if status=='conflict':
                db.execute('UPDATE quote_revision SET status="conflict" WHERE id=?',(qid,))
                db.execute('INSERT INTO quote_conflict(quote_id,source_id,price,digest) VALUES(?,?,?,?)',
                           (qid,data['source_id'],price,quote['digest']))
            decide(db,'quote-identity',qid,status,'same quote ID cannot change price or digest',[data['source_id']])
            out.append({'quote':qid,'status':status});continue
        lic=db.execute('SELECT * FROM stall_licence WHERE id=?',(quote['licence_id'],)).fetchone()
        authorized=bool(lic and lic['status']=='active' and lic['stall']==quote['stall'] and lic['product']==quote['product']
                        and lic['region']==quote['region'] and lic['starts']<=int(quote['day'])<lic['ends'])
        matching=db.execute('SELECT * FROM quote_revision WHERE stall=? AND product=? AND day=? AND version=?',
                            (quote['stall'],quote['product'],int(quote['day']),int(quote['version']))).fetchall()
        conflicting=[r for r in matching if r['price']!=price]
        status='unauthorized' if not authorized else 'conflict' if conflicting else 'valid'
        for prev in conflicting:
            db.execute('UPDATE quote_revision SET status="conflict" WHERE id=?',(prev['id'],))
            decide(db,'quote',prev['id'],'conflict','same stall/version has different price',[qid])
        db.execute('INSERT INTO quote_revision(id,stall,licence_id,product,region,day,version,price,digest,status) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (qid,quote['stall'],quote['licence_id'],quote['product'],quote['region'],int(quote['day']),int(quote['version']),price,quote['digest'],status))
        decide(db,'licence',qid,'authorized' if authorized else 'unauthorized','licence covers product, region, day and stall',[quote['licence_id']])
        decide(db,'quote',qid,status,'same stall/version price consistency',[data['source_id']]+[r['id'] for r in conflicting])
        out.append({'quote':qid,'status':status})
    for qid in data.get('withdraw',[]):
        db.execute('UPDATE quote_revision SET status="withdrawn" WHERE id=?',(qid,))
        decide(db,'quote',qid,'withdrawn','quote withdrawn with past evidence retained',[data['source_id']])
    from .trade import reconcile_all
    reconcile_all(db)
    return {'quotes':out}
