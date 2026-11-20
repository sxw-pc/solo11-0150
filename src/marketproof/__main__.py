import argparse,json
from pathlib import Path
from . import quote,grade,trade,index,report,parties,handover
from .store import connect,read
COMMANDS={'quote-import':quote,'grade-import':grade,'trade-import':trade,'index-publish':index,
          'party-import':parties,'handover-import':handover}
def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=[*COMMANDS,'report','demo'])
    p.add_argument('--db',default='marketproof.sqlite');p.add_argument('--input');p.add_argument('--day',type=int)
    a=p.parse_args()
    db=connect(a.db)
    try:
        if a.command=='report':out=report.build(db)
        elif a.command=='demo':
            root=Path.cwd()/'fixtures';runs=[]
            for command,file in [('quote-import','quote_valid.json'),('grade-import','grade_valid.json'),
                                 ('trade-import','trade_valid.json'),('index-publish','index_valid.json'),
                                 ('quote-import','quote_chain.json'),('grade-import','grade_chain.json'),
                                 ('party-import','parties_chain.json'),('handover-import','handover_chain.json')]:
                with db:runs.append(COMMANDS[command].run(db,read(root/file)))
            with db:runs.append(index.run(db,read(root/'index_chain.json')))
            out={'runs':runs,'legal_vs_price_products':_compare(db),
                 'acceptance_chain':_chain_summary(db),'report':report.build(db)}
        else:
            if not a.input:p.error('--input required')
            with db:out=COMMANDS[a.command].run(db,read(a.input))
            if a.command=='index-publish' and a.day is not None:
                out={'day':a.day,'releases':index.view(db,day=a.day)}
        print(json.dumps({'module_file':__file__,'result':out},ensure_ascii=False,indent=2))
    finally:db.close()

def _chain_summary(db):
    """Same input, three separate decisions for the 10 kg handover chain."""
    from .custody import holder_balances
    trades={r['trade_id']:r['status'] for r in db.execute('SELECT * FROM trade_decision') if r['trade_id'].startswith('t-')}
    products={}
    for line in ('legal','turnover','procurement'):
        row=db.execute('SELECT * FROM index_release WHERE product_line=? AND day=10 ORDER BY id DESC LIMIT 1',(line,)).fetchone()
        if row:
            products[line]={k:row[k] for k in ('status','stall_count','min_price','max_price','median_price','version')}
    samples={line:[{'trade_id':r['trade_id'],'price':r['price']} for r in
                   db.execute('SELECT * FROM daily_sample WHERE product_line=? AND day=10 ORDER BY id',(line,))]
             for line in ('turnover','procurement')}
    return {'lot':'lot-chain','legal_deliveries':trades,'price_products':products,'price_samples':samples,
            'holders':[{'holder':h,'quantity':q} for (l,h),q in sorted(holder_balances(db,lot_id='lot-chain',include_empty=False).items())]}

def _compare(db):
    legal=[dict(r) for r in db.execute('SELECT status,trade_id FROM trade_decision ORDER BY trade_id')]
    products={}
    for line in ('legal','turnover','procurement'):
        row=db.execute('SELECT * FROM index_release WHERE product_line=? ORDER BY id DESC LIMIT 1',(line,)).fetchone()
        if row:
            products[line]={k:row[k] for k in ('status','median_price','min_price','max_price','stall_count','version')}
    from .custody import holder_balances
    return {'legal_deliveries':legal,'price_products':products,
            'holders':[{'lot_id':l,'holder':h,'quantity':q}
                       for (l,h),q in sorted(holder_balances(db,include_empty=False).items())]}
if __name__=='__main__':main()
