"""Barrier-ordered two-connection anomaly scenarios (spec section ①). Sync psycopg, autocommit connections.

Each transaction runs on its own connection and single-thread executor so a statement that blocks on a lock
can be left pending while the other transaction proceeds. A statement still running after PROBE_S is "blocked".
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from workload import LEVELS, begin_sql

PROBE_S = 0.5
STEP_TIMEOUT_S = 15
SCENARIOS = ("lost_update", "write_skew", "double_order", "deadlock", "for_update_decrement")
EXPECTED = {}
for _lvl in LEVELS:
    EXPECTED[("lost_update", _lvl)] = {"anomaly"} if _lvl == "READ COMMITTED" else {"prevented_error"}
    EXPECTED[("write_skew", _lvl)] = {"prevented_error"} if _lvl == "SERIALIZABLE" else {"anomaly"}
    EXPECTED[("double_order", _lvl)] = {"prevented_error"}
    EXPECTED[("deadlock", _lvl)] = {"prevented_error"}
    EXPECTED[("for_update_decrement", _lvl)] = ({"prevented_wait"} if _lvl == "READ COMMITTED"
                                                else {"prevented_wait", "prevented_error"})


def outcome_of(anomaly: bool, errors: list, waited: bool) -> str:
    if anomaly:
        return "anomaly"
    if errors:
        return "prevented_error"
    return "prevented_wait" if waited else "no_anomaly"


def judge(scenario: str, level: str, outcome: str) -> str:
    if outcome in ("not_applicable", "inconclusive"):
        return outcome
    if outcome in EXPECTED[(scenario, level)]:
        return "as_expected"
    return "violation" if outcome == "anomaly" else "differs_no_violation"


class ScenarioHang(RuntimeError):
    pass


class _Tx:
    """One transaction on its own connection; statements run in a worker thread."""

    def __init__(self, connect):
        self.conn = connect()
        self.ex = ThreadPoolExecutor(max_workers=1)
        self.pending = None
        self.error = None          # SQLSTATE (or exception name) of the first failure
        self.waited_ms = 0.0
        self.committed = False

    def _run(self, sql, params):
        cur = self.conn.execute(sql, params)
        return cur.fetchall() if cur.description else cur.rowcount

    def _poll(self, timeout):
        fut, t0, sql = self.pending
        try:
            value = fut.result(timeout=timeout)
        except FutureTimeout:
            return ("blocked", None)
        except Exception as exc:  # noqa: BLE001 - SQL errors are the observation
            self.pending = None
            self._note_wait(t0)
            self.error = getattr(exc, "sqlstate", None) or type(exc).__name__
            return ("error", self.error)
        self.pending = None
        self._note_wait(t0)
        if sql == "COMMIT":
            self.committed = True
        return ("ok", value)

    def _note_wait(self, t0):
        elapsed = time.monotonic() - t0
        if elapsed > PROBE_S:
            self.waited_ms = max(self.waited_ms, elapsed * 1000)

    def do(self, sql, params=None):
        """Run a statement; returns ('ok', value) | ('error', sqlstate) | ('blocked', None) | ('skipped', None)."""
        if self.pending is not None:
            raise RuntimeError("previous statement still pending; call finish() first")
        if self.error is not None:
            return ("skipped", None)
        self.pending = (self.ex.submit(self._run, sql, params), time.monotonic(), sql)
        return self._poll(PROBE_S)

    def finish(self):
        if self.pending is None:
            return ("ok", None)
        status = self._poll(STEP_TIMEOUT_S)
        if status[0] == "blocked":
            raise ScenarioHang("statement blocked beyond STEP_TIMEOUT_S")
        return status

    def close(self):
        try:
            if self.pending is None:
                self.conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.conn.close()
        finally:
            self.ex.shutdown(wait=False)


def _value(status):
    """First column of the first row of an ('ok', rows) status, else None."""
    return status[1][0][0] if status[0] == "ok" and status[1] else None


def _prep(admin, statements):
    for sql in statements:
        admin.execute(sql)


def _result(name, level, anomaly, txs, final):
    errors = [t.error for t in txs if t.error]
    oc = outcome_of(anomaly, errors, any(t.waited_ms > 0 for t in txs))
    return {"scenario": name, "isolation": level, "outcome": oc, "judgement": judge(name, level, oc),
            "sqlstates": errors, "waited_ms": round(max(t.waited_ms for t in txs), 1),
            "committed": [t.committed for t in txs], "final": final, "error": None}


def _begin(a, b, level):
    for t in (a, b):
        st = t.do(begin_sql(level))
        if st[0] == "error":
            return st[1]
    return None


def lost_update(level, a, b, admin):
    _prep(admin, ["CREATE TABLE e004_sc_stock (id int PRIMARY KEY, qty int NOT NULL)",
                  "INSERT INTO e004_sc_stock VALUES (1, 10)"])
    va = _value(a.do("SELECT qty FROM e004_sc_stock WHERE id = 1"))
    vb = _value(b.do("SELECT qty FROM e004_sc_stock WHERE id = 1"))
    a.do("UPDATE e004_sc_stock SET qty = %s WHERE id = 1", (va - 1,))
    b.do("UPDATE e004_sc_stock SET qty = %s WHERE id = 1", (vb - 1,))   # PostgreSQL: blocks on a's row lock
    a.do("COMMIT")
    b.finish()
    b.do("COMMIT")
    final = admin.execute("SELECT qty FROM e004_sc_stock WHERE id = 1").fetchone()[0]
    return a.committed and b.committed and final == 9, final


def write_skew(level, a, b, admin):
    _prep(admin, ["CREATE TABLE e004_sc_acct (id int PRIMARY KEY, bal int NOT NULL)",
                  "INSERT INTO e004_sc_acct VALUES (1, 50), (2, 50)"])
    for t, acct in ((a, 1), (b, 2)):
        total = _value(t.do("SELECT sum(bal) FROM e004_sc_acct"))
        if total is not None and total >= 100:
            t.do("UPDATE e004_sc_acct SET bal = bal - 100 WHERE id = %s", (acct,))
    a.do("COMMIT")
    b.finish()
    b.do("COMMIT")
    final = admin.execute("SELECT sum(bal) FROM e004_sc_acct").fetchone()[0]
    return a.committed and b.committed and final < 0, final


def double_order(level, a, b, admin):
    _prep(admin, ["CREATE TABLE e004_sc_order (id int PRIMARY KEY, op_id text NOT NULL)",
                  "CREATE TABLE e004_sc_receipt (op_id text PRIMARY KEY)"])
    a.do("SELECT op_id FROM e004_sc_receipt WHERE op_id = 'X'")
    b.do("SELECT op_id FROM e004_sc_receipt WHERE op_id = 'X'")
    a.do("INSERT INTO e004_sc_order VALUES (1, 'X')")
    a.do("INSERT INTO e004_sc_receipt VALUES ('X')")
    b.do("INSERT INTO e004_sc_order VALUES (2, 'X')")
    b.do("INSERT INTO e004_sc_receipt VALUES ('X')")                    # PostgreSQL: waits for a, then 23505
    a.do("COMMIT")
    b.finish()
    b.do("COMMIT")
    final = admin.execute("SELECT count(*) FROM e004_sc_order WHERE op_id = 'X'").fetchone()[0]
    return final > 1, final


def deadlock(level, a, b, admin):
    _prep(admin, ["CREATE TABLE e004_sc_pair (id int PRIMARY KEY, v int NOT NULL)",
                  "INSERT INTO e004_sc_pair VALUES (1, 0), (2, 0)"])
    a.do("UPDATE e004_sc_pair SET v = v + 1 WHERE id = 1")
    b.do("UPDATE e004_sc_pair SET v = v + 1 WHERE id = 2")
    a.do("UPDATE e004_sc_pair SET v = v + 1 WHERE id = 2")              # PostgreSQL: blocks on b
    b.do("UPDATE e004_sc_pair SET v = v + 1 WHERE id = 1")              # closes the cycle; detector aborts one
    a.finish()
    a.do("COMMIT")
    b.finish()
    b.do("COMMIT")
    final = admin.execute("SELECT sum(v) FROM e004_sc_pair").fetchone()[0]
    return False, final   # both committing is a valid serial result, never an anomaly


def for_update_decrement(level, a, b, admin):
    _prep(admin, ["CREATE TABLE e004_sc_stock (id int PRIMARY KEY, qty int NOT NULL)",
                  "INSERT INTO e004_sc_stock VALUES (1, 10)"])
    va = _value(a.do("SELECT qty FROM e004_sc_stock WHERE id = 1 FOR UPDATE"))
    first_b = b.do("SELECT qty FROM e004_sc_stock WHERE id = 1 FOR UPDATE")  # PostgreSQL: blocks
    a.do("UPDATE e004_sc_stock SET qty = %s WHERE id = 1", (va - 1,))
    a.do("COMMIT")
    vb = _value(b.finish() if first_b[0] == "blocked" else first_b)
    if vb is not None:
        b.do("UPDATE e004_sc_stock SET qty = %s WHERE id = 1", (vb - 1,))
        b.do("COMMIT")
    final = admin.execute("SELECT qty FROM e004_sc_stock WHERE id = 1").fetchone()[0]
    return a.committed and b.committed and final == 9, final


_FLOWS = {"lost_update": lost_update, "write_skew": write_skew, "double_order": double_order,
          "deadlock": deadlock, "for_update_decrement": for_update_decrement}
_TABLES = ("e004_sc_stock", "e004_sc_acct", "e004_sc_order", "e004_sc_receipt", "e004_sc_pair")


def run_one(name, level, connect):
    admin = connect()
    a = b = None
    try:
        _prep(admin, [f"DROP TABLE IF EXISTS {t}" for t in _TABLES])
        a, b = _Tx(connect), _Tx(connect)
        refused = _begin(a, b, level)
        if refused == "0A000":
            return {"scenario": name, "isolation": level, "outcome": "not_applicable",
                    "judgement": "not_applicable", "sqlstates": [refused], "waited_ms": 0.0,
                    "committed": [False, False], "final": None, "error": None}
        if refused:
            raise RuntimeError(f"BEGIN failed with {refused}")
        anomaly, final = _FLOWS[name](level, a, b, admin)
        return _result(name, level, anomaly, [a, b], final)
    except Exception as exc:  # noqa: BLE001 - recorded, never hidden
        return {"scenario": name, "isolation": level, "outcome": "inconclusive", "judgement": "inconclusive",
                "sqlstates": [], "waited_ms": 0.0, "committed": [], "final": None,
                "error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        for t in (a, b):
            if t is not None:
                t.close()
        try:
            _prep(admin, [f"DROP TABLE IF EXISTS {t}" for t in _TABLES])
        finally:
            admin.close()


def run_all(connect, levels=LEVELS) -> list[dict]:
    return [run_one(s, lvl, connect) for s in SCENARIOS for lvl in levels]
