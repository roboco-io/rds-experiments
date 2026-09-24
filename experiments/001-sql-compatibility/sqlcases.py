"""E001 SQL compatibility cases, error classification and invariant checks.

Pure helpers at the top are testable without psycopg. Every case uses its own
tables and connections in autocommit mode (explicit BEGIN/COMMIT), so one
unsupported statement cannot poison later cases.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

UNSUPPORTED = {"0A000"}
REJECTED = {"42601", "42883", "42704", "42809"}  # syntax/unknown object: review before calling unsupported
SERIOUS = {"unsupported", "rejected", "infra_error", "auth_error", "harness_error"}
UNMEASURED = {
    "orm_schema_migration": "ORM 미선정 — 측정하지 않음",
    "logical_replication_cdc": "CDC 요구 미선정 — 측정하지 않음",
}


def classify_error(sqlstate, *, db_error: bool) -> str:
    if not db_error:
        return "harness_error"
    if not sqlstate:
        return "infra_error"  # connection-level failure without SQLSTATE
    if sqlstate.startswith("28"):
        return "auth_error"
    if sqlstate[:2] in ("08", "53", "58", "XX") or sqlstate in ("57P01", "57P02", "57P03"):
        return "infra_error"
    if sqlstate == "57014":
        return "canceled"
    if sqlstate in UNSUPPORTED:
        return "unsupported"
    if sqlstate in REJECTED:
        return "rejected"
    if sqlstate in ("40001", "40P01"):
        return "conflict"
    if sqlstate == "55P03":
        return "lock_timeout"
    if sqlstate.startswith("23"):
        return "constraint"
    return "sql_error"


def case_outcome(error_class, semantic_ok):
    """Returns (outcome, syntax_supported, semantic_ok)."""
    if error_class is None:
        return ("pass" if semantic_ok else "semantic_mismatch"), True, bool(semantic_ok)
    mapping = {"unsupported": ("unsupported", False), "rejected": ("rejected_needs_review", False),
               "auth_error": ("inconclusive_auth", None), "infra_error": ("inconclusive_infra", None),
               "harness_error": ("harness_error", None)}
    outcome, syntax = mapping.get(error_class, ("error", None))
    return outcome, syntax, None


def lost_update_violations(initial: int, final: int, committed_deltas: list[int]) -> list[str]:
    out = []
    if not committed_deltas:
        out.append("no transaction committed")
    expected = initial + sum(committed_deltas)
    if final != expected:
        out.append(f"final {final} != initial {initial} + committed {committed_deltas}")
    return out


def order_invariant_violations(initial_stock, final_stock, sold, orders, receipts, expected_orders):
    out = []
    for pid, init in initial_stock.items():
        fin = final_stock.get(pid)
        if fin is None or fin < 0:
            out.append(f"product {pid}: negative/missing stock {fin}")
        elif init - fin != sold.get(pid, 0):
            out.append(f"product {pid}: stock delta {init - fin} != sold {sold.get(pid, 0)}")
    if orders != expected_orders:
        out.append(f"orders {orders} != expected {expected_orders}")
    if receipts != orders:
        out.append(f"receipts {receipts} != orders {orders}")
    return out


class SemanticMismatch(Exception):
    pass


@dataclass
class Case:
    name: str
    feature: str
    drops: tuple
    fn: object
    variant_of: str | None = None
    depends_on: str | None = None


CASES: list[Case] = []


def case(name, feature, drops=(), variant_of=None, depends_on=None):
    def deco(fn):
        CASES.append(Case(name, feature, tuple(drops), fn, variant_of, depends_on))
        return fn
    return deco


def _sqlstate(exc):
    return getattr(exc, "sqlstate", None)


def _classify_exc(exc):
    import psycopg
    return classify_error(_sqlstate(exc), db_error=isinstance(exc, psycopg.Error))


class Ctx:
    def __init__(self, connect, deadline_s=30):
        self.connect, self.deadline_s = connect, deadline_s
        self.steps, self.obs, self.conns = [], {}, []
        self.conn = self.new_conn()

    def new_conn(self):
        c = self.connect()
        self.conns.append(c)
        return c

    def exec(self, sql, params=None, conn=None, label=None):
        conn = conn or self.conn
        timer = threading.Timer(self.deadline_s, conn.cancel)  # client-side statement deadline
        t0 = time.monotonic()
        step = {"step": label or sql.split("(")[0][:80], "sql": " ".join(sql.split())[:200]}
        timer.start()
        try:
            cur = conn.execute(sql, params)
            step.update(ok=True)
            return cur
        except Exception as exc:
            step.update(ok=False, sqlstate=_sqlstate(exc), error_class=_classify_exc(exc))
            raise
        finally:
            timer.cancel()
            step["ms"] = round((time.monotonic() - t0) * 1000, 1)
            self.steps.append(step)

    def rows(self, sql, params=None, conn=None):
        return [tuple(r) for r in self.exec(sql, params, conn).fetchall()]

    def scalar(self, sql, params=None, conn=None):
        return self.exec(sql, params, conn).fetchone()[0]

    def check(self, cond, label, **obs):
        self.obs.setdefault("checks", []).append({"check": label, "ok": bool(cond), **obs})
        if not cond:
            raise SemanticMismatch(label)

    def expect_error(self, states, sql, label, params=None, conn=None):
        try:
            self.exec(sql, params, conn, label=label)
        except SemanticMismatch:
            raise
        except Exception as exc:
            if _sqlstate(exc) in states:
                self.steps[-1]["expected"] = True
                return _sqlstate(exc)
            if _classify_exc(exc) in SERIOUS:
                raise
            raise SemanticMismatch(f"{label}: expected {sorted(states)} got {_sqlstate(exc)}") from None
        raise SemanticMismatch(f"{label}: expected {sorted(states)} but statement succeeded")

    def rollback_quiet(self, conn):
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass

    def close(self):
        for c in self.conns:
            try:
                c.close()
            except Exception:
                pass


def run_case(c: Case, connect, sanitize=lambda s: s, deadline_s=30) -> dict:
    t0 = time.monotonic()
    res = {"case": c.name, "feature": c.feature, "variant_of": c.variant_of, "depends_on": c.depends_on}
    ctx, err, semantic_ok, detail = None, None, None, None
    try:
        ctx = Ctx(connect, deadline_s)
        for d in c.drops:  # make reruns idempotent; failures here are recorded only
            try:
                ctx.exec(d, label="pre-drop")
            except Exception:
                ctx.rollback_quiet(ctx.conn)
        c.fn(ctx)
        semantic_ok = True
    except SemanticMismatch as exc:
        semantic_ok, detail = False, str(exc)
    except Exception as exc:
        err = exc
    finally:
        teardown_errors = []
        if ctx is not None:
            for conn in ctx.conns:
                ctx.rollback_quiet(conn)
            for d in c.drops:
                try:
                    ctx.exec(d, label="teardown")
                except Exception as exc:
                    teardown_errors.append(_sqlstate(exc) or type(exc).__name__)
            ctx.close()
    error_class = _classify_exc(err) if err else None
    outcome, syntax, semantic = case_outcome(error_class, semantic_ok)
    res.update(outcome=outcome, syntax_supported=syntax, semantic_ok=semantic, error_class=error_class,
               sqlstate=_sqlstate(err) if err else None,
               message=sanitize(str(err))[:400] if err else detail,
               steps=ctx.steps if ctx else [], observations=ctx.obs if ctx else {},
               teardown_errors=teardown_errors, ms=round((time.monotonic() - t0) * 1000, 1))
    return res


# ---------------------------------------------------------------- cases ----

@case("pk_unique_check", "PK/UNIQUE/CHECK", ["DROP TABLE IF EXISTS e001_puc"])
def _pk(c):
    c.exec("CREATE TABLE e001_puc (id int PRIMARY KEY, code text UNIQUE, qty int NOT NULL CHECK (qty >= 0))")
    c.exec("INSERT INTO e001_puc VALUES (1, 'a', 1)")
    c.expect_error({"23505"}, "INSERT INTO e001_puc VALUES (1, 'b', 1)", "duplicate PK rejected")
    c.expect_error({"23505"}, "INSERT INTO e001_puc VALUES (2, 'a', 1)", "duplicate UNIQUE rejected")
    c.expect_error({"23514"}, "INSERT INTO e001_puc VALUES (3, 'c', -1)", "CHECK rejected")
    c.check(c.scalar("SELECT count(*) FROM e001_puc") == 1, "only valid row persisted")


@case("foreign_key", "FK", ["DROP TABLE IF EXISTS e001_fk_child", "DROP TABLE IF EXISTS e001_fk_parent"])
def _fk(c):
    c.exec("CREATE TABLE e001_fk_parent (id int PRIMARY KEY)")
    c.exec("CREATE TABLE e001_fk_child (id int PRIMARY KEY, "
           "parent_id int NOT NULL REFERENCES e001_fk_parent (id))")
    c.exec("INSERT INTO e001_fk_parent VALUES (1)")
    c.exec("INSERT INTO e001_fk_child VALUES (1, 1)")
    c.expect_error({"23503"}, "INSERT INTO e001_fk_child VALUES (2, 99)", "orphan child rejected")
    c.expect_error({"23503"}, "DELETE FROM e001_fk_parent WHERE id = 1", "referenced parent delete rejected")


@case("sequence", "sequence", ["DROP SEQUENCE IF EXISTS e001_seq"])
def _seq(c):
    c.exec("CREATE SEQUENCE e001_seq")
    a, b = c.scalar("SELECT nextval('e001_seq')"), c.scalar("SELECT nextval('e001_seq')")
    c.check(b > a, "nextval increases", values=[a, b])


@case("sequence_cache_65536", "sequence", ["DROP SEQUENCE IF EXISTS e001_seqc"], variant_of="sequence")
def _seqc(c):
    c.exec("CREATE SEQUENCE e001_seqc CACHE 65536")
    a, b = c.scalar("SELECT nextval('e001_seqc')"), c.scalar("SELECT nextval('e001_seqc')")
    c.check(b > a, "nextval increases", values=[a, b])


@case("identity", "identity", ["DROP TABLE IF EXISTS e001_ident"])
def _ident(c):
    c.exec("CREATE TABLE e001_ident (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, v text)")
    ids = [r[0] for r in c.rows("INSERT INTO e001_ident (v) VALUES ('x'), ('y') RETURNING id")]
    c.check(len(set(ids)) == 2, "identity values distinct", ids=ids)
    c.expect_error({"428C9"}, "INSERT INTO e001_ident (id, v) VALUES (100, 'z')", "GENERATED ALWAYS rejects id")


@case("identity_cache_65536", "identity", ["DROP TABLE IF EXISTS e001_identc"], variant_of="identity")
def _identc(c):
    c.exec("CREATE TABLE e001_identc (id bigint GENERATED BY DEFAULT AS IDENTITY (CACHE 65536) PRIMARY KEY, v text)")
    ids = [r[0] for r in c.rows("INSERT INTO e001_identc (v) VALUES ('x'), ('y') RETURNING id")]
    c.check(len(set(ids)) == 2, "identity values distinct", ids=ids)


@case("serial", "sequence", ["DROP TABLE IF EXISTS e001_serial"])
def _serial(c):
    c.exec("CREATE TABLE e001_serial (id serial PRIMARY KEY, v text)")
    ids = [r[0] for r in c.rows("INSERT INTO e001_serial (v) VALUES ('x'), ('y') RETURNING id")]
    c.check(len(set(ids)) == 2, "serial values distinct", ids=ids)


@case("upsert", "UPSERT", ["DROP TABLE IF EXISTS e001_upsert"])
def _upsert(c):
    c.exec("CREATE TABLE e001_upsert (k text PRIMARY KEY, v int NOT NULL)")
    c.exec("INSERT INTO e001_upsert VALUES ('a', 1)")
    up = "INSERT INTO e001_upsert VALUES (%s, %s) ON CONFLICT (k) DO UPDATE SET v = e001_upsert.v + EXCLUDED.v"
    c.exec(up, ("a", 5))
    c.exec(up, ("b", 2))
    c.exec("INSERT INTO e001_upsert VALUES ('a', 100) ON CONFLICT DO NOTHING")
    got = c.rows("SELECT k, v FROM e001_upsert ORDER BY k")
    c.check(got == [("a", 6), ("b", 2)], "upsert result", got=got)


@case("join_cte_window", "JOIN/CTE/window", ["DROP TABLE IF EXISTS e001_jc_ord", "DROP TABLE IF EXISTS e001_jc_cust"])
def _join(c):
    c.exec("CREATE TABLE e001_jc_cust (id int PRIMARY KEY, name text NOT NULL)")
    c.exec("CREATE TABLE e001_jc_ord (id int PRIMARY KEY, cust_id int NOT NULL, amount int NOT NULL)")
    c.exec("INSERT INTO e001_jc_cust VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    c.exec("INSERT INTO e001_jc_ord VALUES (1, 1, 10), (2, 1, 30), (3, 2, 20), (4, 2, 20), (5, 1, 5)")
    got = c.rows("""WITH t AS (SELECT c.name, o.id, o.amount FROM e001_jc_cust c
                   JOIN e001_jc_ord o ON o.cust_id = c.id)
                 SELECT name, id, amount,
                        row_number() OVER (PARTITION BY name ORDER BY amount DESC, id) AS rn,
                        sum(amount) OVER (PARTITION BY name) AS total
                 FROM t ORDER BY name, rn""")
    want = [("a", 2, 30, 1, 45), ("a", 1, 10, 2, 45), ("a", 5, 5, 3, 45), ("b", 3, 20, 1, 40), ("b", 4, 20, 2, 40)]
    c.check(got == want, "CTE+JOIN+window result", got=got)
    got = c.rows("SELECT c.name, count(o.id) FROM e001_jc_cust c LEFT JOIN e001_jc_ord o "
                 "ON o.cust_id = c.id GROUP BY c.name ORDER BY c.name")
    c.check(got == [("a", 3), ("b", 2), ("c", 0)], "LEFT JOIN aggregate", got=got)


@case("recursive_cte", "JOIN/CTE/window")
def _rcte(c):
    got = c.scalar("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 5) SELECT sum(n) FROM r")
    c.check(got == 15, "recursive sum", got=got)


@case("jsonb_runtime", "JSONB")
def _jrt(c):
    got = c.scalar("""SELECT ('{"a":{"b":2}}'::jsonb #>> '{a,b}')::int""")
    c.check(got == 2, "jsonb path operator", got=got)
    c.check(c.scalar("""SELECT '{"t":["x","y"]}'::jsonb @> '{"t":["x"]}'""") is True, "jsonb containment")


@case("jsonb_column", "JSONB", ["DROP TABLE IF EXISTS e001_json"])
def _jcol(c):
    c.exec("CREATE TABLE e001_json (id int PRIMARY KEY, doc jsonb NOT NULL)")
    c.exec("""INSERT INTO e001_json VALUES (1, '{"sku":"A","tags":["x","y"],"qty":3}'),
                                           (2, '{"sku":"B","tags":["y"],"qty":1}')""")
    got = c.rows("""SELECT id FROM e001_json WHERE doc @> '{"tags":["x"]}' ORDER BY id""")
    c.check(got == [(1,)], "stored jsonb containment", got=got)
    got = c.rows("SELECT doc->>'sku' FROM e001_json WHERE (doc->>'qty')::int > 2")
    c.check(got == [("A",)], "stored jsonb ->>", got=got)


@case("json_text_cast", "JSONB", ["DROP TABLE IF EXISTS e001_jtext"], variant_of="jsonb_column")
def _jtext(c):
    c.exec("CREATE TABLE e001_jtext (id int PRIMARY KEY, doc text NOT NULL)")
    c.exec("""INSERT INTO e001_jtext VALUES (1, '{"sku":"A","qty":3}'), (2, '{"sku":"B","qty":1}')""")
    got = c.rows("SELECT doc::jsonb->>'sku' FROM e001_jtext WHERE (doc::jsonb->>'qty')::int > 2")
    c.check(got == [("A",)], "text column cast to jsonb", got=got)


@case("jsonb_gin_index", "JSONB/index", ["DROP TABLE IF EXISTS e001_jgin"], depends_on="jsonb_column")
def _jgin(c):
    c.exec("CREATE TABLE e001_jgin (id int PRIMARY KEY, doc jsonb NOT NULL)")
    c.exec("CREATE INDEX e001_jgin_doc ON e001_jgin USING gin (doc)")
    c.exec("""INSERT INTO e001_jgin VALUES (1, '{"t":["x"]}'), (2, '{"t":["y"]}')""")
    got = c.rows("""SELECT id FROM e001_jgin WHERE doc @> '{"t":["y"]}'""")
    c.check(got == [(2,)], "gin-indexed containment", got=got)


def _index_case(c, table, create_index_sql, index_name, wait_s=0):
    c.exec(f"CREATE TABLE {table} (id int PRIMARY KEY, cust int NOT NULL, created int NOT NULL, name text NOT NULL)")
    c.exec(f"INSERT INTO {table} SELECT g, g % 10, g, 'N' || g FROM generate_series(1, 200) g")
    c.exec(create_index_sql)
    deadline = time.monotonic() + wait_s
    while True:
        found = c.scalar("SELECT count(*) FROM pg_indexes WHERE indexname = %s", (index_name,))
        if found or time.monotonic() >= deadline:
            break
        time.sleep(2)
    c.check(found == 1, "index visible in pg_indexes", waited_s=wait_s)
    got = c.scalar(f"SELECT count(*) FROM {table} WHERE cust = 3 AND created > 100")
    c.check(got == 10, "query result with index", got=got)


@case("btree_index", "index", ["DROP TABLE IF EXISTS e001_idx"])
def _idx(c):
    _index_case(c, "e001_idx", "CREATE INDEX e001_idx_cust ON e001_idx (cust, created)", "e001_idx_cust")


@case("btree_index_async", "index", ["DROP TABLE IF EXISTS e001_idxa"], variant_of="btree_index")
def _idxa(c):
    _index_case(c, "e001_idxa", "CREATE INDEX ASYNC e001_idxa_cust ON e001_idxa (cust, created)",
                "e001_idxa_cust", wait_s=60)


@case("expression_index", "index", ["DROP TABLE IF EXISTS e001_idxe"])
def _idxe(c):
    _index_case(c, "e001_idxe", "CREATE INDEX e001_idxe_lower ON e001_idxe (lower(name))", "e001_idxe_lower")


@case("temp_table", "temp table")
def _tmp(c):
    c.exec("CREATE TEMP TABLE e001_tmp (id int PRIMARY KEY, v int NOT NULL)")
    c.exec("INSERT INTO e001_tmp VALUES (1, 2), (2, 3)")
    c.check(c.scalar("SELECT sum(v) FROM e001_tmp") == 5, "temp table readable in session")
    c.expect_error({"42P01"}, "SELECT count(*) FROM e001_tmp", "temp table invisible to other session",
                   conn=c.new_conn())


@case("partitioning", "partition", ["DROP TABLE IF EXISTS e001_part_b", "DROP TABLE IF EXISTS e001_part_a",
                                    "DROP TABLE IF EXISTS e001_part"])
def _part(c):
    c.exec("CREATE TABLE e001_part (id int NOT NULL, created date NOT NULL, v int, "
           "PRIMARY KEY (id, created)) PARTITION BY RANGE (created)")
    c.exec("CREATE TABLE e001_part_a PARTITION OF e001_part FOR VALUES FROM ('2026-01-01') TO ('2026-07-01')")
    c.exec("CREATE TABLE e001_part_b PARTITION OF e001_part FOR VALUES FROM ('2026-07-01') TO ('2027-01-01')")
    c.exec("INSERT INTO e001_part VALUES (1, '2026-02-01', 1), (2, '2026-08-01', 1), (3, '2026-09-01', 1)")
    got = c.rows("SELECT tableoid::regclass::text, count(*) FROM e001_part GROUP BY 1 ORDER BY 1")
    c.check(got == [("e001_part_a", 1), ("e001_part_b", 2)], "rows routed to partitions", got=got)
    c.expect_error({"23514"}, "INSERT INTO e001_part VALUES (4, '2030-01-01', 1)", "row without partition rejected")


@case("sql_function", "function", ["DROP FUNCTION IF EXISTS e001_add_tax(int)"])
def _sqlfn(c):
    c.exec("CREATE FUNCTION e001_add_tax(amount int) RETURNS int LANGUAGE sql IMMUTABLE "
           "AS $$ SELECT amount + amount / 10 $$")
    c.check(c.scalar("SELECT e001_add_tax(100)") == 110, "SQL function result")


@case("plpgsql_function", "function", ["DROP FUNCTION IF EXISTS e001_clamp(int)"])
def _plfn(c):
    c.exec("CREATE FUNCTION e001_clamp(v int) RETURNS int LANGUAGE plpgsql "
           "AS $$ BEGIN IF v < 0 THEN RETURN 0; END IF; RETURN v; END $$")
    c.check(c.scalar("SELECT e001_clamp(-5)") == 0 and c.scalar("SELECT e001_clamp(7)") == 7, "plpgsql result")


@case("trigger", "trigger", ["DROP TABLE IF EXISTS e001_trg", "DROP FUNCTION IF EXISTS e001_trg_fn()"],
      depends_on="plpgsql_function")
def _trg(c):
    c.exec("CREATE TABLE e001_trg (id int PRIMARY KEY, v int NOT NULL, version int NOT NULL DEFAULT 0)")
    c.exec("CREATE FUNCTION e001_trg_fn() RETURNS trigger LANGUAGE plpgsql "
           "AS $$ BEGIN NEW.version := OLD.version + 1; RETURN NEW; END $$")
    c.exec("CREATE TRIGGER e001_trg_bu BEFORE UPDATE ON e001_trg FOR EACH ROW EXECUTE FUNCTION e001_trg_fn()")
    c.exec("INSERT INTO e001_trg (id, v) VALUES (1, 1)")
    c.exec("UPDATE e001_trg SET v = 2 WHERE id = 1")
    c.exec("UPDATE e001_trg SET v = 3 WHERE id = 1")
    c.check(c.scalar("SELECT version FROM e001_trg WHERE id = 1") == 2, "trigger incremented version")


def _try_commit(c, conn, stmts):
    """Run stmts + COMMIT; return None on commit, 'stage:sqlstate' on conflict. Serious errors propagate."""
    stage = "begin"
    try:
        for stage, sql, params in stmts:
            c.exec(sql, params, conn)
        stage = "commit"
        c.exec("COMMIT", conn=conn)
        return None
    except Exception as exc:
        c.rollback_quiet(conn)
        if _classify_exc(exc) in SERIOUS:
            raise
        return f"{stage}:{_sqlstate(exc)}"


@case("select_for_update", "SELECT FOR UPDATE", ["DROP TABLE IF EXISTS e001_sfu"])
def _sfu(c):
    c.exec("CREATE TABLE e001_sfu (id int PRIMARY KEY, qty int NOT NULL)")
    c.exec("INSERT INTO e001_sfu VALUES (1, 100)")
    a, b = c.conn, c.new_conn()
    c.exec("BEGIN", conn=a)
    v = c.scalar("SELECT qty FROM e001_sfu WHERE id = 1 FOR UPDATE", conn=a)
    out = {}

    def writer():
        try:
            out["err"] = _try_commit(c, b, [("begin", "BEGIN", None),
                                            ("update", "UPDATE e001_sfu SET qty = qty + 10 WHERE id = 1", None)])
        except Exception as exc:  # re-raised in main thread
            out["exc"] = exc
    th = threading.Thread(target=writer)
    th.start()
    th.join(1.5)
    b_done_while_locked = not th.is_alive()
    a_err = _try_commit(c, a, [("update", "UPDATE e001_sfu SET qty = %s WHERE id = 1", (v + 1,))])
    th.join(c.deadline_s + 5)
    if "exc" in out:
        raise out["exc"]
    deltas = ([1] if a_err is None else []) + ([10] if out.get("err") is None else [])
    final = c.scalar("SELECT qty FROM e001_sfu WHERE id = 1")
    c.obs.update(writer_finished_while_lock_held=b_done_while_locked, locker_error=a_err,
                 writer_error=out.get("err"), final=final)
    c.check(not lost_update_violations(100, final, deltas), "no lost update", deltas=deltas)


def _isolation_case(level):
    def fn(c):
        c.exec("BEGIN")
        c.exec(f"SET TRANSACTION ISOLATION LEVEL {level}")
        got = c.scalar("SHOW transaction_isolation")
        c.exec("COMMIT")
        c.check(got == level.lower(), "requested level is effective", got=got)
    return fn


for _lvl in ("READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"):
    case(f"isolation_{_lvl.lower().replace(' ', '_')}", "isolation")(_isolation_case(_lvl))


@case("rr_lost_update", "isolation", ["DROP TABLE IF EXISTS e001_rr"])
def _rr(c):
    c.exec("CREATE TABLE e001_rr (id int PRIMARY KEY, bal int NOT NULL)")
    c.exec("INSERT INTO e001_rr VALUES (1, 100)")
    a, b = c.conn, c.new_conn()
    vals = {}
    for name, conn in (("a", a), ("b", b)):
        c.exec("BEGIN", conn=conn)
        c.exec("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", conn=conn)
        vals[name] = c.scalar("SELECT bal FROM e001_rr WHERE id = 1", conn=conn)
    a_err = _try_commit(c, a, [("update", "UPDATE e001_rr SET bal = %s WHERE id = 1", (vals["a"] + 10,))])
    b_err = _try_commit(c, b, [("update", "UPDATE e001_rr SET bal = %s WHERE id = 1", (vals["b"] + 20,))])
    final = c.scalar("SELECT bal FROM e001_rr WHERE id = 1")
    deltas = ([10] if a_err is None else []) + ([20] if b_err is None else [])
    c.obs.update(first_error=a_err, second_error=b_err, final=final)
    c.check(not lost_update_violations(100, final, deltas), "no lost update under REPEATABLE READ", deltas=deltas)


@case("statement_timeout", "timeout")
def _sto(c):
    c.exec("SET statement_timeout = '1s'")
    t0 = time.monotonic()
    c.expect_error({"57014"}, "SELECT pg_sleep(3)", "statement_timeout cancels pg_sleep")
    c.check(time.monotonic() - t0 < 2.5, "timeout fired before sleep end")


@case("statement_timeout_cpu", "timeout", variant_of="statement_timeout")
def _stoc(c):
    c.exec("SET statement_timeout = '1s'")
    t0 = time.monotonic()
    c.expect_error({"57014"}, "SELECT count(*) FROM generate_series(1, 100000000)", "statement_timeout cancels scan")
    c.obs["elapsed_s"] = round(time.monotonic() - t0, 2)


@case("client_cancel", "cancel")
def _cancel(c):
    timer = threading.Timer(0.5, c.conn.cancel)
    t0 = time.monotonic()
    timer.start()
    try:
        c.expect_error({"57014"}, "SELECT pg_sleep(5)", "client cancel request honored")
    finally:
        timer.cancel()
    c.check(time.monotonic() - t0 < 4, "cancel returned early")


def _order_case(fk: bool):
    ref = (lambda t: f" REFERENCES {t}") if fk else (lambda t: "")
    p = "e001_ofk" if fk else "e001_op"

    def fn(c):
        c.exec(f"CREATE TABLE {p}_product (id int PRIMARY KEY, price int NOT NULL CHECK (price >= 0))")
        c.exec(f"CREATE TABLE {p}_inventory (product_id int PRIMARY KEY{ref(p + '_product')}, "
               "qty int NOT NULL CHECK (qty >= 0))")
        c.exec(f"CREATE TABLE {p}_orders (id bigint PRIMARY KEY, op_id text NOT NULL UNIQUE, total int NOT NULL)")
        c.exec(f"CREATE TABLE {p}_items (order_id bigint NOT NULL{ref(p + '_orders')}, "
               f"product_id int NOT NULL{ref(p + '_product')}, qty int NOT NULL CHECK (qty > 0), "
               "PRIMARY KEY (order_id, product_id))")
        c.exec(f"CREATE TABLE {p}_receipts (op_id text PRIMARY KEY, order_id bigint NOT NULL)")
        prices, initial = {1: 100, 2: 200, 3: 300}, {1: 5, 2: 5, 3: 1}
        c.exec(f"INSERT INTO {p}_product VALUES (1, 100), (2, 200), (3, 300)")
        c.exec(f"INSERT INTO {p}_inventory VALUES (1, 5), (2, 5), (3, 1)")
        plan = [("op1", {1: 2, 2: 1}), ("op2", {3: 1}), ("op3", {3: 1}), ("op1", {1: 1}),
                ("op4", {1: 3, 2: 1}), ("op5", {1: 1})]
        results = []
        for n, (op, items) in enumerate(plan, start=1):
            results.append(_place_order(c, p, n, op, items, prices))
        c.obs["order_results"] = results
        c.check(results == ["committed", "committed", "rejected_stock", "duplicate", "committed", "rejected_stock"],
                "business outcomes", got=results)
        final = dict(c.rows(f"SELECT product_id, qty FROM {p}_inventory"))
        sold = dict(c.rows(f"SELECT product_id, sum(qty)::int FROM {p}_items GROUP BY product_id"))
        orders = c.scalar(f"SELECT count(*) FROM {p}_orders")
        receipts = c.scalar(f"SELECT count(*) FROM {p}_receipts")
        bad_totals = c.scalar(f"""SELECT count(*) FROM {p}_orders o WHERE o.total <> (SELECT sum(i.qty * pr.price)
                                  FROM {p}_items i JOIN {p}_product pr ON pr.id = i.product_id WHERE i.order_id = o.id)""")
        v = order_invariant_violations(initial, final, sold, orders, receipts, 3)
        c.check(not v and bad_totals == 0, "order invariants", violations=v, bad_totals=bad_totals)
    return fn


def _place_order(c, p, order_id, op, items, prices, attempts=3):
    for _ in range(attempts):
        conn = c.conn
        try:
            c.exec("BEGIN", conn=conn)
            if c.rows(f"SELECT order_id FROM {p}_receipts WHERE op_id = %s", (op,)):
                c.exec("ROLLBACK")
                return "duplicate"
            for pid in sorted(items):
                cur = c.exec(f"UPDATE {p}_inventory SET qty = qty - %s WHERE product_id = %s AND qty >= %s",
                             (items[pid], pid, items[pid]))
                if cur.rowcount != 1:
                    c.exec("ROLLBACK")
                    return "rejected_stock"
            total = sum(prices[pid] * q for pid, q in items.items())
            c.exec(f"INSERT INTO {p}_orders VALUES (%s, %s, %s)", (order_id, op, total))
            for pid, q in sorted(items.items()):
                c.exec(f"INSERT INTO {p}_items VALUES (%s, %s, %s)", (order_id, pid, q))
            c.exec(f"INSERT INTO {p}_receipts VALUES (%s, %s)", (op, order_id))
            c.exec("COMMIT")
            return "committed"
        except Exception as exc:
            c.rollback_quiet(conn)
            cls = _classify_exc(exc)
            if cls == "constraint" and _sqlstate(exc) == "23505":
                return "duplicate"
            if cls != "conflict":
                raise
    return "conflict_exhausted"


_OFK = ["receipts", "items", "orders", "inventory", "product"]
case("order_txn_fk", "common order txn", [f"DROP TABLE IF EXISTS e001_ofk_{t}" for t in _OFK])(_order_case(True))
case("order_txn_portable", "common order txn", [f"DROP TABLE IF EXISTS e001_op_{t}" for t in _OFK],
     variant_of="order_txn_fk")(_order_case(False))


def capture_env(connect, sanitize=lambda s: s) -> dict:
    """Best-effort server facts; each query isolated."""
    env = {}
    queries = {"version": "SELECT version()", "server_version": "SHOW server_version",
               "default_isolation": "SHOW default_transaction_isolation",
               "extensions": "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension"}
    for key, sql in queries.items():
        try:
            conn = connect()
            try:
                env[key] = conn.execute(sql).fetchone()[0]
            finally:
                conn.close()
        except Exception as exc:
            env[key] = {"error": _sqlstate(exc) or type(exc).__name__, "message": sanitize(str(exc))[:200]}
    return env
