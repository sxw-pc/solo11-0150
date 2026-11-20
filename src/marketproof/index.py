"""Two distinct price products over the same handover ledger.

``legal`` reproduces the original daily grade index over confirmed deliveries.
``turnover`` follows every true re-sale and counts only the quantity that finally
reaches an external purchaser: a kilogram circulated back to an earlier holder
contributes nothing, while genuine re-sales can contribute several distinct
stalls.  ``procurement`` keeps only the last sale into an independent external
purchaser.  Both products first take a same-stall price median, then the median
plus min/max across independent stalls, with the pre-existing threshold.
Neither product may invalidate a legal delivery: price exclusions are recorded
only in ``daily_sample``/``index_release``.
"""
from statistics import median
from .store import ingest,decide
from . import parties,custody

LEGAL='legal'
TURNOVER='turnover'
PROCUREMENT='procurement'
PRODUCT_LINES=(LEGAL,TURNOVER,PROCUREMENT)
LINE_LABELS={LEGAL:'legal delivery',TURNOVER:'market turnover',PROCUREMENT:'procurement landed'}

def run(db,data):
    state=ingest(db,'index',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    out=[]
    for request in data['requests']:
        line=request.get('product_line',LEGAL)
        out.append(publish(db,int(request['day']),request['product'],request['grade'],
                           int(request.get('min_stalls',2)),line))
    return {'releases':out}

def recompute_all(db):
    keys=db.execute('SELECT day,product,grade,min_stalls,product_line FROM index_release WHERE id IN '
                    '(SELECT MAX(id) FROM index_release GROUP BY day,product,grade,product_line)').fetchall()
    for key in keys:
        publish(db,key['day'],key['product'],key['grade'],key['min_stalls'],key['product_line'] or LEGAL)

def _samples(db,day,product,grade,line):
    """Return (trade, independent-unit key) pairs eligible for ``line``."""
    trades=db.execute('SELECT * FROM trade_receipt WHERE day=? AND product=? AND grade=? AND status="confirmed" ORDER BY id',
                      (day,product,grade)).fetchall()
    memo={}
    samples=[]
    for t in trades:
        if line==LEGAL:
            samples.append((t,t['stall']));continue
        if int(t['quantity'])<=0:continue
        # Only an independent stall operator's sale is market evidence.
        if not parties.independent(db,t['seller'],t['day']):continue
        key=parties.identity_key(db,t['seller'],t['day'],t['stall'])
        if line==TURNOVER:
            if custody.external_quantity(db,t['id'],memo)<=0:continue
        else:  # procurement: only the last sale that lands with an outside purchaser
            if not parties.is_external(db,t['buyer'],t['day']):continue
            onward=db.execute('SELECT 1 FROM trade_receipt WHERE previous_receipt=? AND status="confirmed" LIMIT 1',
                              (t['id'],)).fetchone()
            if onward:continue
        samples.append((t,key))
    return samples

def publish(db,day,product,grade,min_stalls,product_line=LEGAL):
    if product_line not in PRODUCT_LINES:product_line=LEGAL
    samples=_samples(db,day,product,grade,product_line)
    db.execute('DELETE FROM daily_sample WHERE day=? AND product=? AND grade=? AND product_line=?',
               (day,product,grade,product_line))
    by_unit={}
    for t,unit in samples:
        by_unit.setdefault(unit,[]).append(int(t['price']))
        db.execute('INSERT OR REPLACE INTO daily_sample(day,product,grade,trade_id,stall,price,product_line) VALUES(?,?,?,?,?,?,?)',
                   (day,product,grade,t['id'],unit,t['price'],product_line))
    prices=[median(v) for v in by_unit.values()]
    status='published' if len(prices)>=min_stalls else 'insufficient'
    min_price=min(prices) if prices else None;max_price=max(prices) if prices else None
    med=median(prices) if prices else None
    prior=db.execute('SELECT * FROM index_release WHERE day=? AND product=? AND grade=? AND product_line=? ORDER BY id DESC LIMIT 1',
                     (day,product,grade,product_line)).fetchone()
    values=(status,min_price,max_price,med,len(by_unit),min_stalls)
    line_note=f'{LINE_LABELS[product_line]} line ({product_line})'
    if prior and (prior['status'],prior['min_price'],prior['max_price'],prior['median_price'],prior['stall_count'],prior['min_stalls'])==values:
        return {'release_id':prior['id'],'product_line':product_line,'status':status,
                'stall_count':len(by_unit),'unchanged':True}
    if prior and prior['status']=='published':
        db.execute('INSERT INTO publication_receipt(release_id,status,reason) VALUES(?,?,?)',
                   (prior['id'],'withdrawn',f'{line_note}: upstream quote, grade or delivery chain changed'))
    version=prior['version']+1 if prior else 1
    cur=db.execute('INSERT INTO index_release(day,product,grade,min_stalls,status,min_price,max_price,median_price,stall_count,version,previous,product_line) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (day,product,grade,min_stalls,status,min_price,max_price,med,len(by_unit),version,
                    prior['id'] if prior else None,product_line))
    db.execute('INSERT INTO publication_receipt(release_id,status,reason) VALUES(?,?,?)',
               (cur.lastrowid,status,f'{line_note}: distinct-stall threshold and grade-separated price interval'))
    decide(db,'index',f'{product_line}:{day}:{product}:{grade}',status,
           f'{len(by_unit)} stalls; interval {min_price}..{max_price}',[t['id'] for t,_ in samples])
    return {'release_id':cur.lastrowid,'product_line':product_line,'status':status,'stall_count':len(by_unit),
            'min_price':min_price,'max_price':max_price,'median_price':med}

def view(db,day=None,product=None,grade=None,product_line=None):
    """Latest release per (day, product, grade, product_line), optionally filtered."""
    sql=('SELECT r.* FROM index_release r WHERE r.id IN '
         '(SELECT MAX(id) FROM index_release GROUP BY day,product,grade,product_line)')
    args=[]
    for column,value in (('day',day),('product',product),('grade',grade),('product_line',product_line)):
        if value is not None:sql+=f' AND r.{column}=?';args.append(value)
    sql+=' ORDER BY r.day,r.product,r.grade,r.product_line'
    return [dict(r) for r in db.execute(sql,args)]
