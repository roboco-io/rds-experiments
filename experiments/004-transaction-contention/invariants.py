"""Business invariants checked after every E004 cell (spec section ③)."""
from __future__ import annotations

from workload import INITIAL_BALANCE, INITIAL_STOCK, N_ACCOUNTS

QUERIES = {
    "neg_stock": "SELECT count(*) FROM e004_inventory WHERE qty < 0",
    "stock_mismatch": f"""SELECT count(*) FROM e004_inventory i
        LEFT JOIN (SELECT product_id, sum(qty) AS sold FROM e004_order_items GROUP BY product_id) s
        ON s.product_id = i.product_id WHERE {INITIAL_STOCK} - i.qty <> coalesce(s.sold, 0)""",
    "balance_total": "SELECT coalesce(sum(balance), 0) FROM e004_accounts",
    "account_mismatch": f"""SELECT count(*) FROM e004_accounts a
        LEFT JOIN (SELECT id, sum(d) AS d FROM (SELECT from_id AS id, -amount AS d FROM e004_transfers
                   UNION ALL SELECT to_id, amount FROM e004_transfers) x GROUP BY id) t ON t.id = a.id
        WHERE a.balance <> {INITIAL_BALANCE} + coalesce(t.d, 0)""",
    "neg_balance": "SELECT count(*) FROM e004_accounts WHERE balance < 0",
    "orders": "SELECT count(*) FROM e004_orders",
    "transfers": "SELECT count(*) FROM e004_transfers",
    "orders_without_receipt": """SELECT count(*) FROM e004_orders o
        WHERE NOT EXISTS (SELECT 1 FROM e004_receipts r WHERE r.op_id = o.op_id AND r.kind = 'order')""",
    "transfers_without_receipt": """SELECT count(*) FROM e004_transfers t
        WHERE NOT EXISTS (SELECT 1 FROM e004_receipts r WHERE r.op_id = t.op_id AND r.kind = 'transfer')""",
    "receipts_without_effect": """SELECT count(*) FROM e004_receipts r
        WHERE NOT EXISTS (SELECT 1 FROM e004_orders o WHERE o.op_id = r.op_id)
          AND NOT EXISTS (SELECT 1 FROM e004_transfers t WHERE t.op_id = r.op_id)""",
    "orders_without_items": """SELECT count(*) FROM e004_orders o
        WHERE NOT EXISTS (SELECT 1 FROM e004_order_items i WHERE i.order_id = o.id)""",
    "items_without_order": """SELECT count(*) FROM e004_order_items i
        WHERE NOT EXISTS (SELECT 1 FROM e004_orders o WHERE o.id = i.order_id)""",
    "bad_totals": """SELECT count(*) FROM e004_orders o WHERE o.total <> (SELECT coalesce(sum(i.qty * p.price), 0)
        FROM e004_order_items i JOIN e004_products p ON p.id = i.product_id WHERE i.order_id = o.id)""",
}
_ZERO_REQUIRED = ("neg_stock", "stock_mismatch", "account_mismatch", "orders_without_receipt",
                  "transfers_without_receipt", "receipts_without_effect", "orders_without_items",
                  "items_without_order", "bad_totals")


def collect(conn) -> dict:
    return {k: int(conn.execute(sql).fetchone()[0]) for k, sql in QUERIES.items()}


def check(facts: dict, ledger: dict, isolation: str):
    """Return (violations, observations). Violations void any performance claim for the cell."""
    violations, observations = [], []
    for k in _ZERO_REQUIRED:
        if facts[k]:
            violations.append(f"{k}={facts[k]}")
    expected_total = N_ACCOUNTS * INITIAL_BALANCE
    if facts["balance_total"] != expected_total:
        violations.append(f"balance_total={facts['balance_total']} != {expected_total}")
    if facts["neg_balance"]:
        msg = f"neg_balance={facts['neg_balance']}"
        if isolation == "READ COMMITTED":
            observations.append(msg + " (check-then-act overdraw is permitted under READ COMMITTED)")
        else:
            violations.append(msg)
    for kind, table in (("order", "orders"), ("transfer", "transfers")):
        led = ledger.get(kind, {})
        confirmed, ambiguous = led.get("committed", 0), led.get("ambiguous", 0)
        if facts[table] < confirmed:
            violations.append(f"{table}={facts[table]} < confirmed commits {confirmed} (lost commit)")
        if facts[table] > confirmed + ambiguous:
            violations.append(f"{table}={facts[table]} > confirmed+ambiguous {confirmed + ambiguous} "
                              "(unexpected effects)")
    return violations, observations
