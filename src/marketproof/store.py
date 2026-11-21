"""Append-only source and business decision ledger for synthetic market lots."""
import hashlib,json,sqlite3
from pathlib import Path
SCHEMA="""
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,system TEXT,source_id TEXT,digest TEXT,payload TEXT,business_time INTEGER,received_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(system,source_id,digest));
CREATE TABLE IF NOT EXISTS source_conflict(id INTEGER PRIMARY KEY,system TEXT,source_id TEXT,digest TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS decision(id INTEGER PRIMARY KEY,system TEXT,object_id TEXT,status TEXT,reason TEXT,evidence TEXT,previous INTEGER);
CREATE TABLE IF NOT EXISTS stall_licence(id TEXT PRIMARY KEY,stall TEXT,region TEXT,product TEXT,starts INTEGER,ends INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS quote_revision(id TEXT PRIMARY KEY,stall TEXT,licence_id TEXT,product TEXT,region TEXT,day INTEGER,version INTEGER,price INTEGER,digest TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS quote_conflict(id INTEGER PRIMARY KEY,quote_id TEXT,source_id TEXT,price INTEGER,digest TEXT);
CREATE TABLE IF NOT EXISTS lot_origin(id TEXT PRIMARY KEY,origin TEXT,product TEXT,harvest_day INTEGER,quantity INTEGER,expires_day INTEGER);
CREATE TABLE IF NOT EXISTS inspection_result(id TEXT PRIMARY KEY,lot_id TEXT,origin TEXT,product TEXT,harvest_day INTEGER,grade TEXT,sampled_at INTEGER,version INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS grade_decision(lot_id TEXT PRIMARY KEY,grade TEXT,saleable INTEGER,expires_day INTEGER,status TEXT,version INTEGER);
CREATE TABLE IF NOT EXISTS trade_receipt(id TEXT PRIMARY KEY,quote_id TEXT,lot_id TEXT,stall TEXT,product TEXT,grade TEXT,day INTEGER,price INTEGER,quantity INTEGER,buyer TEXT,seller TEXT,buyer_digest TEXT,seller_digest TEXT,bilateral_ok INTEGER,settled INTEGER,cancelled INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS lot_balance(lot_id TEXT PRIMARY KEY,saleable INTEGER,occupied INTEGER,version INTEGER);
CREATE TABLE IF NOT EXISTS trade_decision(trade_id TEXT PRIMARY KEY,status TEXT,reason TEXT,version INTEGER);
CREATE TABLE IF NOT EXISTS daily_sample(id INTEGER PRIMARY KEY,day INTEGER,product TEXT,grade TEXT,trade_id TEXT,stall TEXT,price INTEGER,UNIQUE(day,product,grade,trade_id));
CREATE TABLE IF NOT EXISTS index_release(id INTEGER PRIMARY KEY,day INTEGER,product TEXT,grade TEXT,min_stalls INTEGER,status TEXT,min_price INTEGER,max_price INTEGER,median_price REAL,stall_count INTEGER,version INTEGER,previous INTEGER);
CREATE TABLE IF NOT EXISTS publication_receipt(id INTEGER PRIMARY KEY,release_id INTEGER,status TEXT,reason TEXT,at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS cold_anchor(id INTEGER PRIMARY KEY,journey TEXT,segment TEXT,source_id TEXT,local_time INTEGER,unified_time INTEGER,offset_minutes INTEGER,business_time INTEGER,UNIQUE(journey,segment,source_id,local_time,unified_time));
CREATE TABLE IF NOT EXISTS cold_reset_visa(id INTEGER PRIMARY KEY,journey TEXT,before_segment TEXT,after_segment TEXT,reset_at INTEGER,source_id TEXT,business_time INTEGER,UNIQUE(journey,source_id));
CREATE TABLE IF NOT EXISTS cold_seal(id INTEGER PRIMARY KEY,journey TEXT,segment TEXT,seal_id TEXT,party TEXT,range_start INTEGER,range_end INTEGER,identity_ok INTEGER,goods_ok INTEGER,alarm_minutes INTEGER,source_id TEXT,business_time INTEGER,UNIQUE(journey,seal_id,source_id));
"""
def connect(path):
    db=sqlite3.connect(path,isolation_level='IMMEDIATE');db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def ingest(db,system,data):
    raw=json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    digest=hashlib.sha256(raw.encode()).hexdigest();sid=data['source_id']
    old=db.execute('SELECT digest FROM source WHERE system=? AND source_id=?',(system,sid)).fetchall()
    if any(r['digest']!=digest for r in old):
        db.execute('INSERT INTO source_conflict(system,source_id,digest,payload) VALUES(?,?,?,?)',(system,sid,digest,raw))
        decide(db,system,sid,'conflict','same source ID changed bytes',[digest]);return 'conflict'
    if old:return 'duplicate'
    db.execute('INSERT INTO source(system,source_id,digest,payload,business_time) VALUES(?,?,?,?,?)',
               (system,sid,digest,raw,int(data.get('business_time',0))))
    return 'new'
def decide(db,system,key,status,reason,evidence):
    ev=json.dumps(evidence,sort_keys=True,ensure_ascii=False)
    old=db.execute('SELECT * FROM decision WHERE system=? AND object_id=? ORDER BY id DESC LIMIT 1',(system,key)).fetchone()
    if old and (old['status'],old['reason'],old['evidence'])==(status,reason,ev):return old['id']
    cur=db.execute('INSERT INTO decision(system,object_id,status,reason,evidence,previous) VALUES(?,?,?,?,?,?)',
                   (system,key,status,reason,ev,old['id'] if old else None));return cur.lastrowid
