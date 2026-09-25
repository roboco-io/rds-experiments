#!/usr/bin/env python3
"""E004 runner CLI (runs on the load-generator EC2, or locally against a DSN target). Prints one JSON line."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone

import conn as C
import invariants as I
import load as L
import scenarios as SC
import workload as W

LOCK_SQL = "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
SATURATION_PCT = 85.0


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, sort_keys=True, default=str)
    os.replace(tmp, path)


def _cpu():
    """(idle+iowait, total) jiffies from /proc/stat, or None off Linux."""
    try:
        with open("/proc/stat") as fh:
            vals = [int(x) for x in fh.readline().split()[1:]]
        return vals[3] + vals[4], sum(vals)
    except OSError:
        return None


def cpu_busy_pct(c0, c1):
    if not c0 or not c1 or c1[1] == c0[1]:
        return None
    return 100.0 * (1 - (c1[0] - c0[0]) / (c1[1] - c0[1]))


class Monitor(threading.Thread):
    """Samples generator CPU and, for PostgreSQL targets, lock waiters once per second in the measure window."""

    def __init__(self, connect, t_measure, t_end):
        super().__init__(daemon=True)
        self.connect, self.t_measure, self.t_end = connect, t_measure, t_end
        self.samples, self.cpu_pct, self.error = [], None, None

    def run(self):
        try:
            time.sleep(max(0.0, self.t_measure - time.time()))
            c0 = _cpu()
            conn = self.connect() if self.connect else None
            while time.time() < self.t_end:
                if conn:
                    self.samples.append(int(conn.execute(LOCK_SQL).fetchone()[0]))
                time.sleep(1)
            self.cpu_pct = cpu_busy_pct(c0, _cpu())
            if conn:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"[:200]

    def result(self):
        s = self.samples
        return {"generator_cpu_pct": self.cpu_pct, "lock_waiters_max": max(s) if s else None,
                "lock_waiters_mean": sum(s) / len(s) if s else None, "lock_samples": len(s), "error": self.error}


def _max_connections(conn):
    try:
        return int(conn.execute("SHOW max_connections").fetchone()[0])
    except Exception:  # noqa: BLE001 - DSQL may not expose it; its connection quota is checked separately
        return None


def _rtt_ms(conn, n=20):
    samples = []
    for _ in range(n):
        t0 = time.monotonic()
        conn.execute("SELECT 1").fetchone()
        samples.append((time.monotonic() - t0) * 1000)
    return sorted(samples)[n // 2]


def cmd_probe(target):
    c = C.sync_connect_factory(target)()
    out = {"max_connections": _max_connections(c)}
    for key, sql in (("server_version", "SHOW server_version"),
                     ("default_isolation", "SHOW default_transaction_isolation")):
        try:
            out[key] = c.execute(sql).fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            out[key] = f"unavailable:{getattr(exc, 'sqlstate', None)}"
    out["rtt_ms"] = _rtt_ms(c)
    c.close()
    return out


def cmd_setup(target):
    t0 = time.monotonic()
    c = C.sync_connect_factory(target)()
    W.setup(c)
    c.close()
    return {"ok": True, "seconds": round(time.monotonic() - t0, 1)}


def cmd_scenarios(target, out):
    results = SC.run_all(C.sync_connect_factory(target))
    _write(out, {"at": _now(), "config": target.get("config"), "results": results})
    return {"ok": True, "n": len(results),
            "violations": sum(r["judgement"] == "violation" for r in results),
            "inconclusive": sum(r["judgement"] == "inconclusive" for r in results)}


def gate_max_connections(target, max_connections):
    """DSQL's connection limit is a cluster quota, not max_connections; do not gate D1 on the SHOW value."""
    return None if target.get("kind") == "dsql" else max_connections


def cmd_cell(target, cell_json, out):
    cell = L.Cell(**json.loads(cell_json))
    result = {"cell_id": cell.cell_id, "cell": asdict(cell), "started_at": _now(), "reason": None}
    admin = None
    try:
        connect = C.sync_connect_factory(target)
        admin = connect()
        result["max_connections"] = _max_connections(admin)
        gate = L.connection_gate(gate_max_connections(target, result["max_connections"]), cell.concurrency)
        if gate:
            result.update(status="not_applicable", reason=gate, ended_at=_now())
            return result
        W.reset(admin)
        result["rtt_ms"] = _rtt_ms(admin)
        t_start = time.time() + L.startup_s(cell.concurrency)
        t_measure = t_start + cell.warmup_s
        mon = Monitor(connect if target["kind"] != "dsql" else None, t_measure, t_measure + cell.measure_s)
        mon.start()
        stats = L.run_cell(target, cell, t_start)
        mon.join(timeout=30)
        facts = I.collect(admin)
        violations, observations = I.check(facts, stats.ledger, cell.isolation)
        sd = stats.to_dict()
        result.update(stats=sd, metrics=L.metrics(sd, cell.measure_s), monitor=mon.result(),
                      invariants={"facts": facts, "violations": violations, "observations": observations},
                      ended_at=_now())
        cpu = result["monitor"]["generator_cpu_pct"]
        result["status"] = "invalid_generator_saturated" if cpu is not None and cpu > SATURATION_PCT else "ok"
        return result
    except Exception as exc:  # noqa: BLE001 - recorded as a failed cell, never hidden
        result.update(status="error", reason=C.redact(f"{type(exc).__name__}: {exc}", target)[:500],
                      ended_at=_now())
        return result
    finally:
        _write(out, result)
        if admin is not None:
            admin.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["probe", "setup", "scenarios", "cell"])
    p.add_argument("--target", required=True)
    p.add_argument("--cell-json")
    p.add_argument("--out")
    a = p.parse_args(argv)
    with open(a.target) as fh:
        target = json.load(fh)
    try:
        if a.command == "probe":
            res = cmd_probe(target)
        elif a.command == "setup":
            res = cmd_setup(target)
        elif a.command == "scenarios":
            res = cmd_scenarios(target, a.out)
        else:
            full = cmd_cell(target, a.cell_json, a.out)
            m = full.get("metrics") or {}
            res = {"cell_id": full["cell_id"], "status": full["status"], "reason": full["reason"],
                   "success_tps": m.get("success_tps"),
                   "violations": len((full.get("invariants") or {}).get("violations", []))}
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": C.redact(f"{type(exc).__name__}: {exc}", target)[:500]}))
        sys.exit(1)
    print(json.dumps(res, default=str))


if __name__ == "__main__":
    main()
