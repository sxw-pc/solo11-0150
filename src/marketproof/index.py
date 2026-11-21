"""Distinct-stall daily grade prices with withdrawal receipts."""
from statistics import median
from .store import ingest,decide

def run(db,data):
    state=ingest(db,'index',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    out=[]
    for request in data['requests']:
        out.append(publish(db,int(request['day']),request['product'],request['grade'],int(request.get('min_stalls',2))))
    return {'releases':out}

def recompute_all(db):
    keys=db.execute('SELECT day,product,grade,min_stalls FROM index_release WHERE id IN '
                    '(SELECT MAX(id) FROM index_release GROUP BY day,product,grade)').fetchall()
    for key in keys:publish(db,key['day'],key['product'],key['grade'],key['min_stalls'])

def publish(db,day,product,grade,min_stalls):
    trades=db.execute('SELECT * FROM trade_receipt WHERE day=? AND product=? AND grade=? AND status="confirmed" ORDER BY id',
                      (day,product,grade)).fetchall()
    db.execute('DELETE FROM daily_sample WHERE day=? AND product=? AND grade=?',(day,product,grade))
    by_stall={}
    for t in trades:
        by_stall.setdefault(t['stall'],[]).append(t['price'])
        db.execute('INSERT INTO daily_sample(day,product,grade,trade_id,stall,price) VALUES(?,?,?,?,?,?)',
                   (day,product,grade,t['id'],t['stall'],t['price']))
    prices=[median(v) for v in by_stall.values()]
    status='published' if len(prices)>=min_stalls else 'insufficient'
    min_price=min(prices) if prices else None;max_price=max(prices) if prices else None
    med=median(prices) if prices else None
    prior=db.execute('SELECT * FROM index_release WHERE day=? AND product=? AND grade=? ORDER BY id DESC LIMIT 1',
                     (day,product,grade)).fetchone()
    values=(status,min_price,max_price,med,len(by_stall),min_stalls)
    if prior and (prior['status'],prior['min_price'],prior['max_price'],prior['median_price'],prior['stall_count'],prior['min_stalls'])==values:
        return {'release_id':prior['id'],'status':status,'stall_count':len(by_stall),'unchanged':True}
    if prior and prior['status']=='published':
        db.execute('INSERT INTO publication_receipt(release_id,status,reason) VALUES(?,?,?)',
                   (prior['id'],'withdrawn','upstream quote, grade or trade changed'))
    version=prior['version']+1 if prior else 1
    cur=db.execute('INSERT INTO index_release(day,product,grade,min_stalls,status,min_price,max_price,median_price,stall_count,version,previous) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (day,product,grade,min_stalls,status,min_price,max_price,med,len(by_stall),version,prior['id'] if prior else None))
    db.execute('INSERT INTO publication_receipt(release_id,status,reason) VALUES(?,?,?)',
               (cur.lastrowid,status,'distinct-stall threshold and grade-separated price interval'))
    decide(db,'index',f'{day}:{product}:{grade}',status,f'{len(by_stall)} stalls; interval {min_price}..{max_price}',[t['id'] for t in trades])
    return {'release_id':cur.lastrowid,'status':status,'stall_count':len(by_stall),'min_price':min_price,'max_price':max_price,'median_price':med}
