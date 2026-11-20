"""Signed party register: operators, beneficial owners, stalls and purchasers.

Identity is only ever taken from a registration whose signed digest matches the
receipt digest and whose effective period contains the handover day.  Stall
names never imply independence: independence and beneficial ownership come
from the registration record.
"""
from .store import ingest,decide

TRADER='trader'
PURCHASER='purchaser'

def run(db,data):
    state=ingest(db,'party',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    out=[]
    for reg in data.get('registrations',[]):
        rid=reg['id']
        fields=(reg['party'],reg['kind'],int(bool(reg.get('independent',True))),reg.get('stall'),
                reg.get('owner') or reg['party'],reg['registration'],reg['digest'],
                int(reg['starts']),int(reg['ends']))
        old=db.execute('SELECT * FROM party_register WHERE id=?',(rid,)).fetchone()
        if old and tuple(old[k] for k in ('party','kind','independent','stall','owner','registration','digest','starts','ends'))!=fields:
            decide(db,'party-identity',rid,'conflict','signed registration cannot silently change',[data['source_id']])
            out.append({'registration':rid,'status':'conflict'});continue
        db.execute('INSERT OR IGNORE INTO party_register(id,party,kind,independent,stall,owner,registration,digest,starts,ends) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (rid,*fields))
        decide(db,'party-register',rid,'registered','effective signed identity',[data['source_id']])
        out.append({'registration':rid,'status':'registered'})
    from .trade import reconcile_all
    reconcile_all(db)
    return {'registrations':out}

def effective(db,party,day,digest=None,kind=None,stall=None):
    """Return the signed registration in force on ``day`` for ``party``.

    ``stall`` filters on the registered stall only when a non-empty stall was
    registered; a purchaser or an operator without a stall registration can
    still transact, but cannot silently claim a different stall number.
    """
    if not party:return None
    rows=db.execute('SELECT * FROM party_register WHERE party=? AND starts<=? AND ends>? ORDER BY starts DESC,id DESC',
                    (party,int(day),int(day))).fetchall()
    if kind is not None:rows=[r for r in rows if r['kind']==kind]
    if stall is not None:
        mismatched=[r for r in rows if r['stall'] and r['stall']!=stall]
        rows=[r for r in rows if not r['stall'] or r['stall']==stall]
        if not rows and mismatched:return None
    if digest is not None:
        signed=[r for r in rows if r['digest']==digest]
        if not signed:return None
        rows=signed
    return rows[0] if rows else None

def is_external(db,party,day):
    """A purchaser outside the stall ring; never inferred from a stall name."""
    if not party:return False
    reg=effective(db,party,day)
    return bool(reg and reg['kind']!=TRADER)

def identity_key(db,party,day,stall=None):
    """Independent-market unit: beneficial owner when registered, else the stall."""
    reg=effective(db,party,day)
    if reg is None:return stall or party
    return reg['owner'] or reg['party']

def independent(db,party,day):
    reg=effective(db,party,day)
    return bool(reg is None or reg['independent'])
