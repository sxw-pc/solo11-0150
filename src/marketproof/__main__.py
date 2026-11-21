import argparse,json
from pathlib import Path
from . import quote,grade,trade,index,report,cold_intake
from .store import connect,read
COMMANDS={'quote-import':quote,'grade-import':grade,'trade-import':trade,'index-publish':index,'cold-import':cold_intake}
COLD_COMMANDS={
    'cold-anchor-import':cold_intake.run_anchor,
    'cold-seal-import':cold_intake.run_seal,
    'cold-reset-import':cold_intake.run_reset,
    'cold-attribute':cold_intake.run_attribute,
    'cold-recommend':cold_intake.run_recommend,
}
def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=[*COMMANDS,'report','demo',*COLD_COMMANDS])
    p.add_argument('--db',default='marketproof.sqlite');p.add_argument('--input');a=p.parse_args()
    db=connect(a.db)
    try:
        if a.command=='report':out=report.build(db)
        elif a.command=='demo':
            root=Path.cwd()/'fixtures';runs=[]
            for command,file in [('quote-import','quote_valid.json'),('grade-import','grade_valid.json'),
                                 ('trade-import','trade_valid.json'),('index-publish','index_valid.json')]:
                with db:runs.append(COMMANDS[command].run(db,read(root/file)))
            out={'runs':runs,'report':report.build(db)}
        elif a.command in COLD_COMMANDS:
            if not a.input:p.error('--input required')
            with db:out=COLD_COMMANDS[a.command](db,read(a.input))
        else:
            if not a.input:p.error('--input required')
            with db:out=COMMANDS[a.command].run(db,read(a.input))
        print(json.dumps({'module_file':__file__,'result':out},ensure_ascii=False,indent=2))
    finally:db.close()
if __name__=='__main__':main()
