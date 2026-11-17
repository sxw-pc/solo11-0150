"""Origin identity, inspection grade, freshness and late reinspection."""
from .store import ingest,decide

def run(db,data):
    state=ingest(db,'grade',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    for lot in data.get('lots',[]):
        old=db.execute('SELECT * FROM lot_origin WHERE id=?',(lot['id'],)).fetchone()
        fields=(lot['origin'],lot['product'],int(lot['harvest_day']),int(lot['quantity']),int(lot['expires_day']))
        if old and tuple(old[k] for k in ('origin','product','harvest_day','quantity','expires_day'))!=fields:
            decide(db,'origin',lot['id'],'conflict','lot master cannot silently change',[data['source_id']]);continue
        db.execute('INSERT OR IGNORE INTO lot_origin(id,origin,product,harvest_day,quantity,expires_day) VALUES(?,?,?,?,?,?)',
                   (lot['id'],*fields))
    out=[]
    for inspection in data.get('inspections',[]):
        iid=inspection['id'];lot=db.execute('SELECT * FROM lot_origin WHERE id=?',(inspection['lot_id'],)).fetchone()
        valid=bool(lot and lot['origin']==inspection['origin'] and lot['product']==inspection['product']
                   and lot['harvest_day']==int(inspection['harvest_day']))
        old=db.execute('SELECT id FROM inspection_result WHERE id=?',(iid,)).fetchone()
        if old:continue
        db.execute('INSERT INTO inspection_result(id,lot_id,origin,product,harvest_day,grade,sampled_at,version,status) VALUES(?,?,?,?,?,?,?,?,?)',
                   (iid,inspection['lot_id'],inspection['origin'],inspection['product'],int(inspection['harvest_day']),inspection['grade'],int(inspection['sampled_at']),int(inspection['version']),'valid' if valid else 'wrong_lot'))
        decide(db,'inspection',iid,'valid' if valid else 'wrong_lot','origin, product and harvest day must match',[inspection['lot_id']])
        out.append({'inspection':iid,'status':'valid' if valid else 'wrong_lot'})
    for lot_id in {i['lot_id'] for i in data.get('inspections',[])}|{x['id'] for x in data.get('lots',[])}:
        recompute_lot(db,lot_id,int(data.get('business_time',0)))
    from .trade import reconcile_all
    reconcile_all(db)
    return {'inspections':out}

def recompute_lot(db,lot_id,day):
    lot=db.execute('SELECT * FROM lot_origin WHERE id=?',(lot_id,)).fetchone()
    if not lot:return
    rows=db.execute('SELECT * FROM inspection_result WHERE lot_id=? AND status="valid" ORDER BY sampled_at DESC,version DESC',(lot_id,)).fetchall()
    top=rows[0] if rows else None
    tie=bool(top and any(r['sampled_at']==top['sampled_at'] and r['grade']!=top['grade'] for r in rows[1:]))
    if tie:status='disputed';grade=None
    elif not top:status='pending_inspection';grade=None
    elif lot['expires_day']<day:status='expired';grade=top['grade']
    else:status='saleable';grade=top['grade']
    quantity=lot['quantity'] if status=='saleable' else 0
    old=db.execute('SELECT * FROM grade_decision WHERE lot_id=?',(lot_id,)).fetchone()
    version=old['version'] if old and (old['grade'],old['saleable'],old['status'])==(grade,quantity,status) else old['version']+1 if old else 1
    db.execute('INSERT INTO grade_decision(lot_id,grade,saleable,expires_day,status,version) VALUES(?,?,?,?,?,?) '
               'ON CONFLICT(lot_id) DO UPDATE SET grade=excluded.grade,saleable=excluded.saleable,expires_day=excluded.expires_day,status=excluded.status,version=excluded.version',
               (lot_id,grade,quantity,lot['expires_day'],status,version))
    decide(db,'grade',lot_id,status,f'grade {grade}; saleable {quantity}; version {version}',[r['id'] for r in rows])
