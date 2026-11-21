"""Origin identity, inspection grade, freshness and late reinspection."""
import json
from .store import ingest,decide

REVIEW_SCHEMA="""
CREATE TABLE IF NOT EXISTS lot_independent_review(lot_id TEXT PRIMARY KEY,enabled INTEGER,source_id TEXT);
CREATE TABLE IF NOT EXISTS review_context(lot_id TEXT PRIMARY KEY,round TEXT,offsets TEXT,thresholds TEXT,registered_range TEXT,source_id TEXT,business_time INTEGER);
CREATE TABLE IF NOT EXISTS review_sample(lot_id TEXT,specimen TEXT,sample_id TEXT,round TEXT,reading INTEGER,preserved INTEGER,operator TEXT,witness TEXT,claim TEXT,source_id TEXT,PRIMARY KEY(lot_id,specimen));
CREATE TABLE IF NOT EXISTS review_reference(lot_id TEXT,reference_id TEXT,kind TEXT,true_value INTEGER,threshold INTEGER,round TEXT,available INTEGER,preserved INTEGER,source_id TEXT,PRIMARY KEY(lot_id,reference_id));
CREATE TABLE IF NOT EXISTS review_verification(lot_id TEXT,verification_id TEXT,day INTEGER,procedure TEXT,round TEXT,payload TEXT,status TEXT,grade TEXT,conclusion TEXT,witness TEXT,PRIMARY KEY(lot_id,verification_id),UNIQUE(lot_id,day));
CREATE TABLE IF NOT EXISTS review_decision(lot_id TEXT PRIMARY KEY,grade TEXT,status TEXT,reason TEXT,evidence TEXT,version INTEGER);
"""

def ensure_review_schema(db):
    db.executescript(REVIEW_SCHEMA)

def review_enabled(db,lot_id):
    ensure_review_schema(db)
    row=db.execute('SELECT enabled FROM lot_independent_review WHERE lot_id=?',(lot_id,)).fetchone()
    return bool(row and row['enabled'])

def record_review_optin(db,lot,source_id):
    enabled=1 if lot.get('independent_review') else 0
    old=db.execute('SELECT enabled FROM lot_independent_review WHERE lot_id=?',(lot['id'],)).fetchone()
    if old and old['enabled']!=enabled:
        decide(db,'review-optin',lot['id'],'conflict','independent review adoption cannot silently change',[source_id]);return
    db.execute('INSERT OR IGNORE INTO lot_independent_review(lot_id,enabled,source_id) VALUES(?,?,?)',
               (lot['id'],enabled,source_id))

def apply_review_result(db,lot_id,grade,status,reason,evidence,day):
    ensure_review_schema(db)
    ev=json.dumps(evidence,sort_keys=True,ensure_ascii=False)
    old=db.execute('SELECT * FROM review_decision WHERE lot_id=?',(lot_id,)).fetchone()
    version=old['version'] if old and (old['grade'],old['status'],old['reason'],old['evidence'])==(grade,status,reason,ev) else old['version']+1 if old else 1
    db.execute('INSERT INTO review_decision(lot_id,grade,status,reason,evidence,version) VALUES(?,?,?,?,?,?) '
               'ON CONFLICT(lot_id) DO UPDATE SET grade=excluded.grade,status=excluded.status,reason=excluded.reason,evidence=excluded.evidence,version=excluded.version',
               (lot_id,grade,status,reason,ev,version))
    decide(db,'review',lot_id,status,reason,evidence)
    recompute_lot(db,lot_id,day)
    from .trade import reconcile_all
    reconcile_all(db)

def run(db,data):
    ensure_review_schema(db)
    state=ingest(db,'grade',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    for lot in data.get('lots',[]):
        old=db.execute('SELECT * FROM lot_origin WHERE id=?',(lot['id'],)).fetchone()
        fields=(lot['origin'],lot['product'],int(lot['harvest_day']),int(lot['quantity']),int(lot['expires_day']))
        if old and tuple(old[k] for k in ('origin','product','harvest_day','quantity','expires_day'))!=fields:
            decide(db,'origin',lot['id'],'conflict','lot master cannot silently change',[data['source_id']]);continue
        db.execute('INSERT OR IGNORE INTO lot_origin(id,origin,product,harvest_day,quantity,expires_day) VALUES(?,?,?,?,?,?)',
                   (lot['id'],*fields))
        if 'independent_review' in lot:
            record_review_optin(db,lot,data['source_id'])
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
    review=db.execute('SELECT * FROM review_decision WHERE lot_id=?',(lot_id,)).fetchone()
    rows=db.execute('SELECT * FROM inspection_result WHERE lot_id=? AND status="valid" ORDER BY sampled_at DESC,version DESC',(lot_id,)).fetchall()
    top=rows[0] if rows else None
    if review and review['status']=='resolved':
        grade=review['grade'];status='expired' if lot['expires_day']<day else 'saleable'
    elif review and review['status']=='disputed':
        grade=None;status='disputed'
    else:
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
