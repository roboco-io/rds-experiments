"""E004 schema, deterministic seed data, reset, and business operations.

The same SQL runs on every service. It follows E001's portable (FK-free) order transaction.
Every modifying statement touches at most MAX_ROWS_PER_TXN rows so DSQL's per-transaction row quota is never hit
(re-check the quota on the run date: https://docs.aws.amazon.com/aurora-dsql/latest/userguide/CHAP_quotas.html).
"""
from __future__ import annotations

from dataclasses import dataclass

N_PRODUCTS = 10_000
N_ACCOUNTS = 1_000
INITIAL_STOCK = 1_000_000      # never exhausted within one cell
INITIAL_BALANCE = 1_000_000
HOT_FRACTION = 0.01            # top 1% of keys ...
HOT_SHARE = 0.8                # ... receive 80% of accesses under the "hot" distribution
ORDER_SHARE = 0.7              # 70% order creation / 30% account transfer
MAX_ROWS_PER_TXN = 1000
LEVELS = ("READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE")

TABLES = ("e004_order_items", "e004_orders", "e004_transfers", "e004_receipts",
          "e004_inventory", "e004_accounts", "e004_products")
SCHEMA = (
    "CREATE TABLE e004_products (id int PRIMARY KEY, price int NOT NULL CHECK (price >= 0))",
    "CREATE TABLE e004_inventory (product_id int PRIMARY KEY, qty int NOT NULL CHECK (qty >= 0))",
    "CREATE TABLE e004_orders (id bigint PRIMARY KEY, op_id text NOT NULL UNIQUE, cell text NOT NULL, "
    "total int NOT NULL)",
    "CREATE TABLE e004_order_items (order_id bigint NOT NULL, product_id int NOT NULL, "
    "qty int NOT NULL CHECK (qty > 0), cell text NOT NULL, PRIMARY KEY (order_id, product_id))",
    "CREATE TABLE e004_receipts (op_id text PRIMARY KEY, kind text NOT NULL, ref_id bigint NOT NULL, "
    "cell text NOT NULL)",
    "CREATE TABLE e004_accounts (id int PRIMARY KEY, balance bigint NOT NULL)",
    "CREATE TABLE e004_transfers (id bigint PRIMARY KEY, op_id text NOT NULL UNIQUE, cell text NOT NULL, "
    "from_id int NOT NULL, to_id int NOT NULL, amount int NOT NULL)",
)

SEL_RECEIPT = "SELECT ref_id FROM e004_receipts WHERE op_id = %s"
DEC_STOCK = "UPDATE e004_inventory SET qty = qty - %s WHERE product_id = %s AND qty >= %s"
INS_ORDER = "INSERT INTO e004_orders (id, op_id, cell, total) VALUES (%s, %s, %s, %s)"
INS_ITEM = "INSERT INTO e004_order_items (order_id, product_id, qty, cell) VALUES (%s, %s, %s, %s)"
INS_RECEIPT = "INSERT INTO e004_receipts (op_id, kind, ref_id, cell) VALUES (%s, %s, %s, %s)"
SEL_BALANCE = "SELECT balance FROM e004_accounts WHERE id = %s"
ADD_BALANCE = "UPDATE e004_accounts SET balance = balance + %s WHERE id = %s"
INS_TRANSFER = ("INSERT INTO e004_transfers (id, op_id, cell, from_id, to_id, amount) "
                "VALUES (%s, %s, %s, %s, %s, %s)")

# (table, select-keys SQL, delete template, keys per batch, max rows deleted per key)
DELETE_PLANS = (
    ("e004_order_items", "SELECT DISTINCT order_id FROM e004_order_items LIMIT %s",
     "DELETE FROM e004_order_items WHERE order_id IN ({})", 400, 2),
    ("e004_orders", "SELECT id FROM e004_orders LIMIT %s", "DELETE FROM e004_orders WHERE id IN ({})", 1000, 1),
    ("e004_transfers", "SELECT id FROM e004_transfers LIMIT %s",
     "DELETE FROM e004_transfers WHERE id IN ({})", 1000, 1),
    ("e004_receipts", "SELECT op_id FROM e004_receipts LIMIT %s",
     "DELETE FROM e004_receipts WHERE op_id IN ({})", 1000, 1),
)


def price(pid: int) -> int:
    return 100 + pid % 900


def begin_sql(isolation: str) -> str:
    if isolation not in LEVELS:
        raise ValueError(f"unknown isolation level {isolation!r}")
    return f"BEGIN ISOLATION LEVEL {isolation}"


def _insert(table, cols, rows):
    ph = "(" + ", ".join(["%s"] * len(cols)) + ")"
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES " + ", ".join([ph] * len(rows))
    return sql, [v for r in rows for v in r]


def _ranges(n):
    for lo in range(1, n + 1, MAX_ROWS_PER_TXN):
        yield lo, min(lo + MAX_ROWS_PER_TXN - 1, n)


def seed_batches():
    for lo, hi in _ranges(N_PRODUCTS):
        ids = range(lo, hi + 1)
        yield _insert("e004_products", ("id", "price"), [(i, price(i)) for i in ids])
        yield _insert("e004_inventory", ("product_id", "qty"), [(i, INITIAL_STOCK) for i in ids])
    for lo, hi in _ranges(N_ACCOUNTS):
        yield _insert("e004_accounts", ("id", "balance"), [(i, INITIAL_BALANCE) for i in range(lo, hi + 1)])


def restore_batches():
    for lo, hi in _ranges(N_PRODUCTS):
        yield "UPDATE e004_inventory SET qty = %s WHERE product_id BETWEEN %s AND %s", (INITIAL_STOCK, lo, hi)
    for lo, hi in _ranges(N_ACCOUNTS):
        yield "UPDATE e004_accounts SET balance = %s WHERE id BETWEEN %s AND %s", (INITIAL_BALANCE, lo, hi)


def setup(conn) -> None:
    """Drop and recreate the schema, then seed. conn: sync psycopg connection with autocommit=True."""
    for t in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    for ddl in SCHEMA:
        conn.execute(ddl)
    for sql, params in seed_batches():
        conn.execute(sql, params)


def reset(conn) -> None:
    """Delete all business rows in quota-sized batches and restore stock/balances to the seed values."""
    for _table, select_sql, delete_tpl, limit, _rows in DELETE_PLANS:
        while True:
            keys = [r[0] for r in conn.execute(select_sql, (limit,)).fetchall()]
            if not keys:
                break
            conn.execute(delete_tpl.format(", ".join(["%s"] * len(keys))), keys)
    for sql, params in restore_batches():
        conn.execute(sql, params)


@dataclass(frozen=True)
class Op:
    kind: str                 # "order" | "transfer"
    op_id: str                # business ID; unique per cell run
    ref_id: int               # order/transfer primary key
    cell: str
    items: tuple = ()         # ((product_id, qty), ...) sorted by product_id
    transfer: tuple = ()      # (from_id, to_id, amount)


def choose_key(rng, n: int, dist: str) -> int:
    hot_n = max(1, int(n * HOT_FRACTION))
    if dist == "uniform":
        return rng.randint(1, n)
    if dist != "hot":
        raise ValueError(f"unknown distribution {dist!r}")
    return rng.randint(1, hot_n) if rng.random() < HOT_SHARE else rng.randint(hot_n + 1, n)


def ref_id(worker: int, seq: int) -> int:
    if not (0 <= worker < 2 ** 20 and 0 <= seq < 2 ** 40):
        raise ValueError("worker/seq out of range")
    return (worker << 40) | seq


def make_op(rng, dist: str, cell: str, worker: int, seq: int) -> Op:
    rid, op_id = ref_id(worker, seq), f"{cell}:{worker}:{seq}"
    if rng.random() < ORDER_SHARE:
        want, pids = rng.randint(1, 2), set()
        while len(pids) < want:
            pids.add(choose_key(rng, N_PRODUCTS, dist))
        return Op("order", op_id, rid, cell, items=tuple((p, rng.randint(1, 3)) for p in sorted(pids)))
    a = choose_key(rng, N_ACCOUNTS, dist)
    b = a
    while b == a:
        b = choose_key(rng, N_ACCOUNTS, dist)
    return Op("transfer", op_id, rid, cell, transfer=(a, b, rng.randint(1, 100)))
