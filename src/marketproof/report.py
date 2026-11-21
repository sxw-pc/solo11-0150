"""Recompute current market conclusions alongside immutable input history."""
def build(db):
    names=['stall_licence','quote_revision','quote_conflict','lot_origin','inspection_result','grade_decision','trade_receipt','lot_balance','trade_decision','daily_sample','index_release','publication_receipt','decision']
    return {n:[dict(row) for row in db.execute(f'SELECT * FROM {n} ORDER BY 1')] for n in names}
