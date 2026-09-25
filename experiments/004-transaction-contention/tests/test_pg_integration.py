"""Integration test against a real PostgreSQL 16. Run:
docker run -d --rm --name e004-pg -e POSTGRES_PASSWORD=e004 -p 55432:5432 postgres:16
E004_PG_DSN=postgresql://postgres:e004@localhost:55432/postgres python3 -m unittest tests.test_pg_integration -v
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import conn as C  # noqa: E402
import invariants as I  # noqa: E402
import load as L  # noqa: E402
import scenarios as SC  # noqa: E402
import workload as W  # noqa: E402

DSN = os.environ.get("E004_PG_DSN")


@unittest.skipUnless(DSN, "set E004_PG_DSN to run")
class PostgresIntegration(unittest.TestCase):
    target = {"kind": "dsn", "dsn": DSN or ""}

    def test_scenarios_match_postgres_semantics(self):
        results = SC.run_all(C.sync_connect_factory(self.target))
        bad = [(r["scenario"], r["isolation"], r["outcome"], r["error"]) for r in results
               if r["judgement"] != "as_expected"]
        self.assertEqual(bad, [])
        dl = next(r for r in results if r["scenario"] == "deadlock" and r["isolation"] == "READ COMMITTED")
        self.assertIn("40P01", dl["sqlstates"])

    def test_small_cell_keeps_invariants(self):
        conn = C.sync_connect_factory(self.target)()
        W.setup(conn)
        W.reset(conn)
        for retry_ in ("none", "retry3"):
            cell = L.Cell("R1", "REPEATABLE READ", "hot", 8, retry_, 1, warmup_s=1, measure_s=3)
            W.reset(conn)
            stats = L.run_cell(self.target, cell, time.time() + 2, processes=2)
            v, _obs = I.check(I.collect(conn), stats.ledger, cell.isolation)
            self.assertEqual(v, [], retry_)
            self.assertGreater(stats.ledger["order"]["committed"], 0)
            m = L.metrics(stats.to_dict(), cell.measure_s)
            self.assertIsNotNone(m["p99_ms"])
        conn.close()


if __name__ == "__main__":
    unittest.main()
