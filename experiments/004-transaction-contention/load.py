"""Fixed-concurrency contention load for one E004 cell, plus the pure cell plan and metrics.

Runner side: run_cell() spawns processes; each runs asyncio workers, one psycopg AsyncConnection per worker.
Latency = op start to final outcome, including retries and backoff. Only ops started inside the measure window
count toward metrics; the ledger counts every op of the cell because invariants compare against the whole DB.
"""
from __future__ import annotations

import asyncio
import os
import random
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from multiprocessing import get_context

import psycopg

import hist
import retry
import workload as W

ISOLATION_SHORT = {"READ COMMITTED": "RC", "REPEATABLE READ": "RR", "SERIALIZABLE": "SER"}
CONCURRENCY = (16, 64, 256)
DISTS = ("uniform", "hot")
CONN_RESERVE = 10          # admin + monitor connections kept free
STAGGER_S = 0.02           # delay between connection opens inside one process


@dataclass(frozen=True)
class Cell:
    config: str
    isolation: str
    dist: str
    concurrency: int
    retry: str
    rep: int
    warmup_s: int = 30
    measure_s: int = 60

    @property
    def cell_id(self) -> str:
        return (f"{self.config}-{ISOLATION_SHORT[self.isolation]}-{self.dist}-c{self.concurrency}"
                f"-{self.retry}-r{self.rep}")

    @property
    def seed(self) -> int:
        return zlib.crc32(self.cell_id.encode())


def plan_cells(config: str, reps: int = 3, warmup_s: int = 30, measure_s: int = 60) -> list[Cell]:
    base = [("REPEATABLE READ", d, c, r) for d in DISTS for c in CONCURRENCY for r in ("none", "retry3")]
    if config != "D1":  # controls' default level; D1's default is REPEATABLE READ (reuse the common matrix)
        base += [("READ COMMITTED", d, c, "retry3") for d in DISTS for c in CONCURRENCY]
    cells = []
    for rep in range(1, reps + 1):
        order = list(base)
        random.Random(f"e004-{config}-rep{rep}").shuffle(order)
        cells += [Cell(config, iso, d, c, r, rep, warmup_s, measure_s) for iso, d, c, r in order]
    return cells


def connection_gate(max_connections, concurrency: int):
    if max_connections is None or concurrency + CONN_RESERVE <= max_connections:
        return None
    return f"max_connections={max_connections} < concurrency {concurrency} + reserve {CONN_RESERVE}"


def startup_s(concurrency: int) -> float:
    return 10 + concurrency * 0.05


@dataclass
class Result:
    outcome: str                     # committed | rejected_stock | rejected_funds | failed
    attempts: int
    errors: list = field(default_factory=list)
    ambiguous: bool = False          # commit may have happened and could not be resolved
    resolved: bool = False           # an ambiguous commit was confirmed through the receipt
    reason: str | None = None


def _new_kind():
    return {"outcomes": {}, "attempts": 0, "attempt_errors": {}, "reasons": {}, "resolved_ambiguous": 0,
            "latency_all": hist.LogHistogram(), "latency_committed": hist.LogHistogram()}


def _add(d, key, n=1):
    d[key] = d.get(key, 0) + n


class Stats:
    def __init__(self):
        self.kinds = {}
        self.ledger = {"order": {"committed": 0, "ambiguous": 0}, "transfer": {"committed": 0, "ambiguous": 0}}
        self.ops_total = 0
        self.attempts_total = 0

    def record(self, kind: str, res: Result, latency_ms: float, in_measure: bool) -> None:
        self.ops_total += 1
        self.attempts_total += res.attempts
        if res.outcome == "committed":
            self.ledger[kind]["committed"] += 1
        if res.ambiguous:
            self.ledger[kind]["ambiguous"] += 1
        if not in_measure:
            return
        k = self.kinds.setdefault(kind, _new_kind())
        _add(k["outcomes"], res.outcome)
        k["attempts"] += res.attempts
        for e in res.errors:
            _add(k["attempt_errors"], retry.classify(e))
        if res.reason:
            _add(k["reasons"], res.reason)
        if res.resolved:
            k["resolved_ambiguous"] += 1
        k["latency_all"].record(latency_ms)
        if res.outcome == "committed":
            k["latency_committed"].record(latency_ms)

    def merge(self, other: "Stats") -> None:
        for kind, o in other.kinds.items():
            k = self.kinds.setdefault(kind, _new_kind())
            for name in ("outcomes", "attempt_errors", "reasons"):
                for key, n in o[name].items():
                    _add(k[name], key, n)
            k["attempts"] += o["attempts"]
            k["resolved_ambiguous"] += o["resolved_ambiguous"]
            k["latency_all"].merge(o["latency_all"])
            k["latency_committed"].merge(o["latency_committed"])
        for kind, led in other.ledger.items():
            for key, n in led.items():
                self.ledger[kind][key] += n
        self.ops_total += other.ops_total
        self.attempts_total += other.attempts_total

    def to_dict(self) -> dict:
        kinds = {n: {**{key: v for key, v in k.items() if not key.startswith("latency_")},
                     "latency_all": k["latency_all"].to_dict(),
                     "latency_committed": k["latency_committed"].to_dict()} for n, k in self.kinds.items()}
        return {"kinds": kinds, "ledger": self.ledger, "ops_total": self.ops_total,
                "attempts_total": self.attempts_total}

    @classmethod
    def from_dict(cls, d: dict) -> "Stats":
        s = cls()
        for n, k in d["kinds"].items():
            s.kinds[n] = {**k, "latency_all": hist.LogHistogram.from_dict(k["latency_all"]),
                          "latency_committed": hist.LogHistogram.from_dict(k["latency_committed"])}
        s.ledger = {kind: dict(v) for kind, v in d["ledger"].items()}
        s.ops_total, s.attempts_total = d["ops_total"], d["attempts_total"]
        return s


def metrics(stats: dict, measure_s: float) -> dict:
    def summarize(kinds):
        outcomes, errs, attempts, lat = {}, {}, 0, hist.LogHistogram()
        for k in kinds:
            for key, n in k["outcomes"].items():
                _add(outcomes, key, n)
            for key, n in k["attempt_errors"].items():
                _add(errs, key, n)
            attempts += k["attempts"]
            lat.merge(hist.LogHistogram.from_dict(k["latency_all"]))
        ops = sum(outcomes.values())
        committed, failed = outcomes.get("committed", 0), outcomes.get("failed", 0)
        conflicts = errs.get("serialization", 0) + errs.get("deadlock", 0)
        return {"ops": ops, "committed": committed, "success_tps": round(committed / measure_s, 3),
                "final_failure_rate": failed / ops if ops else None,
                "raw_conflict_rate": conflicts / attempts if attempts else None,
                "business_rejects": outcomes.get("rejected_stock", 0) + outcomes.get("rejected_funds", 0),
                "attempts": attempts, "attempt_errors": errs,
                "p50_ms": lat.percentile(50), "p95_ms": lat.percentile(95), "p99_ms": lat.percentile(99),
                "min_ms": lat.min, "max_ms": lat.max}
    out = summarize(stats["kinds"].values())
    out["per_kind"] = {n: summarize([k]) for n, k in stats["kinds"].items()}
    return out


# ------------------------------------------------------------ runner side --

class ConnHolder:
    def __init__(self, connect):
        self.connect = connect
        self.conn = None
        self.in_commit = False
        self.reconnects = 0

    async def open(self):
        self.conn = await self.connect()

    async def close(self):
        try:
            await self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    async def reopen(self):
        await self.close()
        self.reconnects += 1
        self.conn = await self.connect()

    async def rollback_quiet(self):
        try:
            await self.conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - connection unusable: replace it
            await self.reopen()


async def _commit(h):
    h.in_commit = True
    await h.conn.execute("COMMIT")
    h.in_commit = False


async def order_txn(h, op, isolation):
    c = h.conn
    await c.execute(W.begin_sql(isolation))
    if await (await c.execute(W.SEL_RECEIPT, (op.op_id,))).fetchone():
        await c.execute("ROLLBACK")
        return "duplicate"
    for pid, qty in op.items:
        cur = await c.execute(W.DEC_STOCK, (qty, pid, qty))
        if cur.rowcount != 1:
            await c.execute("ROLLBACK")
            return "rejected_stock"
    total = sum(W.price(pid) * qty for pid, qty in op.items)
    await c.execute(W.INS_ORDER, (op.ref_id, op.op_id, op.cell, total))
    for pid, qty in op.items:
        await c.execute(W.INS_ITEM, (op.ref_id, pid, qty, op.cell))
    await c.execute(W.INS_RECEIPT, (op.op_id, "order", op.ref_id, op.cell))
    await _commit(h)
    return "committed"


async def transfer_txn(h, op, isolation):
    c = h.conn
    src, dst, amount = op.transfer
    await c.execute(W.begin_sql(isolation))
    if await (await c.execute(W.SEL_RECEIPT, (op.op_id,))).fetchone():
        await c.execute("ROLLBACK")
        return "duplicate"
    row = await (await c.execute(W.SEL_BALANCE, (src,))).fetchone()
    if row is None or row[0] < amount:
        await c.execute("ROLLBACK")
        return "rejected_funds"
    await c.execute(W.ADD_BALANCE, (-amount, src))
    await c.execute(W.ADD_BALANCE, (amount, dst))
    await c.execute(W.INS_TRANSFER, (op.ref_id, op.op_id, op.cell, src, dst, amount))
    await c.execute(W.INS_RECEIPT, (op.op_id, "transfer", op.ref_id, op.cell))
    await _commit(h)
    return "committed"


TXNS = {"order": order_txn, "transfer": transfer_txn}


async def _receipt_exists(h, op):
    """After an ambiguous commit: reconnect and look up the business ID. None = could not tell."""
    try:
        await h.reopen()
        return (await (await h.conn.execute(W.SEL_RECEIPT, (op.op_id,))).fetchone()) is not None
    except Exception:  # noqa: BLE001
        return None


async def run_op(h, op, isolation, policy, rng) -> Result:
    t0 = time.monotonic()
    errors, attempt = [], 0
    after_ambiguous = False   # a lost COMMIT looked uncommitted; it may still land before our retry
    while True:
        attempt += 1
        remaining = policy.deadline_s - (time.monotonic() - t0)
        if remaining <= 0:
            return Result("failed", attempt - 1, errors, reason="deadline")
        try:
            out = await asyncio.wait_for(TXNS[op.kind](h, op, isolation), remaining)
        except asyncio.TimeoutError:
            errors.append("timeout")
            was_commit, h.in_commit = h.in_commit, False
            state = await _receipt_exists(h, op)       # also replaces the interrupted connection
            if state:
                return Result("committed", attempt, errors, resolved=True)
            return Result("failed", attempt, errors, ambiguous=was_commit and state is None, reason="deadline")
        except psycopg.Error as exc:
            st = exc.sqlstate
            was_commit, h.in_commit = h.in_commit, False
            conn_error = st is None or st.startswith("08")
            errors.append(st or "conn")
            if conn_error:
                if not retry.is_ambiguous(st, was_commit):
                    try:
                        await h.reopen()
                    except Exception:  # noqa: BLE001
                        pass
                    return Result("failed", attempt, errors, reason="connection")
                state = await _receipt_exists(h, op)
                if state:
                    return Result("committed", attempt, errors, resolved=True)
                if state is None:
                    return Result("failed", attempt, errors, ambiguous=True, reason="unresolved")
                after_ambiguous = True
                retry_as = "40001"                        # not visible yet: retry like a conflict
            elif st == "23505" and after_ambiguous:      # the earlier COMMIT landed late and owns the receipt
                await h.rollback_quiet()
                return Result("committed", attempt, errors, resolved=True)
            else:
                await h.rollback_quiet()
                retry_as = st
            delay = policy.next_delay(attempt, retry_as, time.monotonic() - t0, rng)
            if delay is None:
                return Result("failed", attempt, errors, reason=retry.classify(st))
            await asyncio.sleep(delay)
            continue
        if out == "duplicate":  # op IDs are unique per cell: only our own earlier attempt can own the receipt
            out = "committed" if attempt > 1 else "failed"
        return Result(out, attempt, errors, reason=None if out != "failed" else "duplicate")


async def _sleep_until(t):
    delay = t - time.time()
    if delay > 0:
        await asyncio.sleep(delay)


async def _worker(wid, cell, connect, t_start, stats):
    rng = random.Random(f"{cell.cell_id}:{wid}")
    policy = retry.POLICIES[cell.retry]
    t_measure = t_start + cell.warmup_s
    t_end = t_measure + cell.measure_s
    h = ConnHolder(connect)
    await h.open()
    await _sleep_until(t_start)
    seq = 0
    while time.time() < t_end:
        op = W.make_op(rng, cell.dist, cell.cell_id, wid, seq)
        seq += 1
        started_wall, m0 = time.time(), time.monotonic()
        res = await run_op(h, op, cell.isolation, policy, rng)
        stats.record(op.kind, res, (time.monotonic() - m0) * 1000, t_measure <= started_wall < t_end)
    await h.close()


def _proc_entry(args):
    target, cell_dict, wids, t_start = args
    import conn as C
    cell = Cell(**cell_dict)
    connect = C.async_connect_factory(target)
    stats = Stats()

    async def main():
        tasks = []
        for wid in wids:
            tasks.append(asyncio.create_task(_worker(wid, cell, connect, t_start, stats)))
            await asyncio.sleep(STAGGER_S)
        await asyncio.gather(*tasks)
    asyncio.run(main())
    return stats.to_dict()


def run_cell(target: dict, cell: Cell, t_start: float, processes: int | None = None) -> Stats:
    processes = max(1, min(processes or os.cpu_count() or 1, cell.concurrency))
    groups = [list(range(i, cell.concurrency, processes)) for i in range(processes)]
    with ProcessPoolExecutor(processes, mp_context=get_context("spawn")) as ex:
        parts = list(ex.map(_proc_entry, [(target, asdict(cell), g, t_start) for g in groups]))
    total = Stats()
    for p in parts:
        total.merge(Stats.from_dict(p))
    return total
