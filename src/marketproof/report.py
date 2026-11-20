"""Recompute current market conclusions alongside immutable input history."""
def build(db):
    names=['stall_licence','quote_revision','quote_conflict','lot_origin','inspection_result','grade_decision',
           'party_register','trade_receipt','lot_balance','trade_decision','transfer_void','custody_position',
           'daily_sample','index_release','publication_receipt','decision']
    view={n:[dict(row) for row in db.execute(f'SELECT * FROM {n} ORDER BY 1')] for n in names}
    from .custody import holder_balances
    view['holder_balances']=[{'lot_id':lot,'holder':holder,'quantity':qty}
                             for (lot,holder),qty in sorted(holder_balances(db,include_empty=False).items())]
    view['price_products']={
        'market_turnover':_latest(db,'turnover'),
        'procurement_landed':_latest(db,'procurement'),
        'legal_delivery':_latest(db,'legal'),
    }
    return view

def _latest(db,line):
    rows=db.execute('SELECT * FROM index_release WHERE product_line=? AND id IN '
                    '(SELECT MAX(id) FROM index_release WHERE product_line=? GROUP BY day,product,grade) ORDER BY day,product,grade',
                    (line,line)).fetchall()
    return [dict(r) for r in rows]
