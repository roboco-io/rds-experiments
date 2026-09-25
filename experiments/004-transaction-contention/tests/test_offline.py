"""Offline tests: no AWS or database access. Run: python3 -m unittest discover -s tests -v"""
import os
import random
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import safety as S  # noqa: E402

ACCT = "123456789012"
PFX = "e004-20260925t010203z-ab12"


def _manifest(tmp, lifetime=600):
    return S.Manifest.create(os.path.join(tmp, "m.json"), ACCT, S.REGION, PFX, lifetime, "arn:aws:iam::x:user/op")


class Safeguards(unittest.TestCase):
    def test_prefix_and_identity(self):
        self.assertTrue(S.new_run_prefix().startswith("e004-"))
        S.validate_run_prefix(PFX)
        for bad in ("e001-20260925t010203z-ab12", "e004-bad"):
            with self.assertRaises(S.SafetyError):
                S.validate_run_prefix(bad)
        S.assert_identity(ACCT, {"Account": ACCT})
        with self.assertRaises(S.SafetyError):
            S.assert_identity(ACCT, {"Account": "999999999999"})

    def test_manifest_private_and_lifetime_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            self.assertEqual(stat.S_IMODE(os.stat(m.path).st_mode), 0o600)
            self.assertEqual(m.data["experiment"], "E004")
            with self.assertRaises(S.SafetyError):
                _manifest(tmp)  # already exists
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(S.SafetyError):
                _manifest(tmp, lifetime=S.MAX_LIFETIME_MIN + 1)

    def test_scope_types_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            with self.assertRaises(S.SafetyError):
                m.add_resource("D1", "vpc", "vpc-1")
            m.add_resource(S.BATCH, "vpc", "vpc-1", state="created")

    def test_owned_requires_exact_tags(self):
        tags = S.resource_tags(PFX, "R1", "2026-09-25T10:00:00Z")
        self.assertTrue(S.owned(tags, PFX, "R1"))
        self.assertFalse(S.owned(tags, PFX, "A1"))
        self.assertFalse(S.owned({**tags, S.TAG_MANAGED: "other"}, PFX))


class Ordering(unittest.TestCase):
    def test_batch_first_then_one_config_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            with self.assertRaises(S.SafetyError):
                S.check_can_provision(m.data, "D1")  # batch not ready
            S.check_can_provision(m.data, S.BATCH)
            m.add_resource(S.BATCH, "vpc", "vpc-1", state="created")
            with self.assertRaises(S.SafetyError):
                S.check_can_provision(m.data, S.BATCH)  # batch exists
            m.data["config_status"][S.BATCH] = "ready"
            S.check_can_provision(m.data, "D1")
            with self.assertRaises(S.SafetyError):
                S.check_can_provision(m.data, "R1")  # D1 not cleaned
            S.check_can_provision(m.data, "R1", allow_order_override=True)
            m.add_resource("D1", "dsql_cluster", "c1", state="created")
            with self.assertRaises(S.SafetyError):
                S.check_can_provision(m.data, "R1", allow_order_override=True)  # D1 active

    def test_cleanup_order_and_batch_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            for t, i in (("vpc", "v"), ("client_security_group", "c"), ("db_security_group", "d"),
                         ("iam_role", "r"), ("instance_profile", "p"), ("ec2_instance", "i"),
                         ("internet_gateway", "g"), ("subnet", "s"), ("db_subnet_group", "n")):
                m.add_resource(S.BATCH, t, i, state="created")
            m.add_resource("R1", "db_instance", "db", state="created")
            with self.assertRaises(S.SafetyError):
                S.cleanup_plan(m.data, S.BATCH)  # R1 still active
            m.set_state("R1", "db_instance", "db", "deleted")
            order = [r["type"] for r in S.cleanup_plan(m.data, S.BATCH)]
            self.assertEqual(order, ["ec2_instance", "instance_profile", "iam_role", "db_subnet_group",
                                     "db_security_group", "client_security_group", "subnet",
                                     "internet_gateway", "vpc"])
            m.add_resource("A1", "rds_secret", "arn:s", state="created")
            m.add_resource("A1", "db_instance", "w1", state="created")
            m.add_resource("A1", "db_cluster", "c", state="created")
            self.assertEqual([r["type"] for r in S.cleanup_plan(m.data, "A1")],
                             ["db_instance", "db_cluster", "rds_secret"])


import cost as C  # noqa: E402


class Cost(unittest.TestCase):
    NOW = datetime(2026, 9, 25, 13, 0, tzinfo=timezone.utc)

    def _data(self):
        return {"dsql_dpu_usd": 0.5, "resources": [
            {"config": "R1", "type": "db_instance", "id": "db", "state": "deleted",
             "recorded_at": "2026-09-25T10:00:00Z", "deleted_at": "2026-09-25T12:00:00Z",
             "extra": {"rate_usd_per_h": 0.203}},
            {"config": "BATCH", "type": "ec2_instance", "id": "i", "state": "created",
             "recorded_at": "2026-09-25T10:00:00Z", "extra": {"rate_usd_per_h": 0.1}},
            {"config": "BATCH", "type": "vpc", "id": "v", "state": "created",
             "recorded_at": "2026-09-25T10:00:00Z", "extra": {}},
        ]}

    def test_spent_includes_deleted_active_and_dpu(self):
        self.assertAlmostEqual(C.spent_usd(self._data(), self.NOW), 0.406 + 0.3 + 0.5, places=6)
        self.assertAlmostEqual(C.active_rate(self._data()), 0.1)

    def test_guard_stops_before_cap(self):
        g = C.guard(self._data(), 90, now=self.NOW)
        self.assertAlmostEqual(g["next_cell_usd"], 0.1 * 90 / 3600)
        self.assertAlmostEqual(g["reserve_usd"], 0.05)
        self.assertTrue(g["ok"])
        self.assertFalse(C.guard(self._data(), 90, cap=1.25, now=self.NOW)["ok"])

    def test_guard_counts_dpu_estimate(self):
        g = C.guard(self._data(), 90, est_cell_dpu=2_000_000, dpu_usd_per_million=1.0, now=self.NOW)
        self.assertAlmostEqual(g["next_cell_usd"], 0.1 * 90 / 3600 + 2.0)
        self.assertFalse(C.guard(self._data(), 90, est_cell_dpu=4_000_000, dpu_usd_per_million=1.0,
                                 now=self.NOW)["ok"])

    def test_measured_cost_replaces_worst_case_until_measured_point(self):
        r = {"recorded_at": "2026-09-25T10:00:00Z", "state": "created",
             "extra": {"rate_usd_per_h": 1.04, "measured_usd": 0.5, "measured_until": "2026-09-25T12:00:00Z"}}
        self.assertAlmostEqual(C.resource_usd(r, self.NOW), 0.5 + 1.04)


import retry as R  # noqa: E402


class Retry(unittest.TestCase):
    def test_policies(self):
        self.assertEqual(R.POLICIES["none"].max_attempts, 1)
        self.assertEqual(R.POLICIES["retry3"].max_attempts, 3)
        self.assertEqual(R.POLICIES["retry3"].deadline_s, 2.0)

    def test_next_delay(self):
        rng = random.Random(1)
        p = R.POLICIES["retry3"]
        self.assertIsNone(p.next_delay(1, "23505", 0.0, rng))           # not retryable
        self.assertIsNone(R.POLICIES["none"].next_delay(1, "40001", 0.0, rng))
        d1 = p.next_delay(1, "40001", 0.0, rng)
        self.assertTrue(0 <= d1 <= 0.01)
        d2 = p.next_delay(2, "40P01", 0.0, rng)
        self.assertTrue(0 <= d2 <= 0.02)
        self.assertIsNone(p.next_delay(3, "40001", 0.0, rng))           # attempts exhausted
        fixed = type("FixedRng", (), {"uniform": lambda self, a, b: 0.005})()
        self.assertEqual(p.next_delay(1, "40001", 1.99, fixed), 0.005)
        self.assertIsNone(p.next_delay(1, "40001", 1.996, fixed))       # sleep would cross the 2 s deadline

    def test_classify_and_ambiguity(self):
        self.assertEqual(R.classify("40001"), "serialization")
        self.assertEqual(R.classify("40P01"), "deadlock")
        self.assertEqual(R.classify("23505"), "unique_violation")
        self.assertEqual(R.classify(None), "connection")
        self.assertEqual(R.classify("08006"), "connection")
        self.assertEqual(R.classify("timeout"), "timeout")
        self.assertEqual(R.classify("42P01"), "other")
        self.assertTrue(R.is_ambiguous(None, True))
        self.assertTrue(R.is_ambiguous("08006", True))
        self.assertFalse(R.is_ambiguous(None, False))
        self.assertFalse(R.is_ambiguous("40001", True))


import hist as H  # noqa: E402


class Histogram(unittest.TestCase):
    def test_percentiles_within_one_percent(self):
        h = H.LogHistogram()
        for ms in range(1, 1001):
            h.record(float(ms))
        self.assertEqual(h.count, 1000)
        self.assertAlmostEqual(h.percentile(50), 500, delta=500 * 0.011)
        self.assertAlmostEqual(h.percentile(99), 990, delta=990 * 0.011)
        self.assertEqual(h.percentile(100), 1000)
        self.assertEqual((h.min, h.max), (1.0, 1000.0))

    def test_merge_roundtrip_empty(self):
        a, b = H.LogHistogram(), H.LogHistogram()
        self.assertIsNone(a.percentile(50))
        a.record(5)
        b.record(50)
        a.merge(b)
        c = H.LogHistogram.from_dict(a.to_dict())
        self.assertEqual((c.count, c.min, c.max), (2, 5, 50))
        self.assertEqual(c.percentile(100), 50)
        with self.assertRaises(ValueError):
            a.merge(H.LogHistogram(precision=0.05))


import re  # noqa: E402
import workload as W  # noqa: E402


class Workload(unittest.TestCase):
    def test_batches_respect_row_quota_and_cover_all_rows(self):
        rows = {"e004_products": 0, "e004_inventory": 0, "e004_accounts": 0}
        for sql, params in W.seed_batches():
            table = re.search(r"INSERT INTO (\w+)", sql).group(1)
            ncols = len(re.search(r"\(([^)]*)\) VALUES", sql).group(1).split(","))
            n = len(params) // ncols
            self.assertLessEqual(n, W.MAX_ROWS_PER_TXN)
            rows[table] += n
        self.assertEqual(rows, {"e004_products": W.N_PRODUCTS, "e004_inventory": W.N_PRODUCTS,
                                "e004_accounts": W.N_ACCOUNTS})
        covered = 0
        for sql, (value, lo, hi) in W.restore_batches():
            self.assertLessEqual(hi - lo + 1, W.MAX_ROWS_PER_TXN)
            covered += hi - lo + 1
        self.assertEqual(covered, W.N_PRODUCTS + W.N_ACCOUNTS)
        for _table, _sel, _tpl, limit, rows_per_key in W.DELETE_PLANS:
            self.assertLessEqual(limit * rows_per_key, W.MAX_ROWS_PER_TXN)

    def test_hot_distribution(self):
        rng = random.Random(7)
        hot_n = int(W.N_PRODUCTS * W.HOT_FRACTION)
        draws = [W.choose_key(rng, W.N_PRODUCTS, "hot") for _ in range(20000)]
        share = sum(k <= hot_n for k in draws) / len(draws)
        self.assertAlmostEqual(share, 0.8, delta=0.02)
        uni = [W.choose_key(rng, W.N_PRODUCTS, "uniform") for _ in range(20000)]
        self.assertAlmostEqual(sum(k <= hot_n for k in uni) / len(uni), 0.01, delta=0.005)
        self.assertTrue(all(1 <= k <= W.N_PRODUCTS for k in draws + uni))
        with self.assertRaises(ValueError):
            W.choose_key(rng, 10, "zipf")

    def test_ops_are_well_formed_and_unique(self):
        rng = random.Random(3)
        ops = [W.make_op(rng, "hot", "cellX", w, s) for w in range(4) for s in range(500)]
        kinds = [o.kind for o in ops]
        self.assertAlmostEqual(kinds.count("order") / len(ops), 0.7, delta=0.03)
        self.assertEqual(len({o.ref_id for o in ops}), len(ops))
        self.assertEqual(len({o.op_id for o in ops}), len(ops))
        for o in ops:
            if o.kind == "order":
                pids = [p for p, _ in o.items]
                self.assertEqual(pids, sorted(set(pids)))
                self.assertTrue(all(1 <= q <= 3 for _, q in o.items))
            else:
                a, b, amt = o.transfer
                self.assertNotEqual(a, b)
                self.assertTrue(1 <= amt <= 100)
        with self.assertRaises(ValueError):
            W.ref_id(-1, 0)

    def test_begin_sql(self):
        self.assertEqual(W.begin_sql("REPEATABLE READ"), "BEGIN ISOLATION LEVEL REPEATABLE READ")
        with self.assertRaises(ValueError):
            W.begin_sql("repeatable read; DROP TABLE x")


import invariants as I  # noqa: E402


def _clean_facts(orders=10, transfers=5):
    f = {k: 0 for k in I.QUERIES}
    f.update(balance_total=W.N_ACCOUNTS * W.INITIAL_BALANCE, orders=orders, transfers=transfers)
    return f


class Invariants(unittest.TestCase):
    LED = {"order": {"committed": 10, "ambiguous": 0}, "transfer": {"committed": 5, "ambiguous": 0}}

    def test_clean(self):
        self.assertEqual(I.check(_clean_facts(), self.LED, "REPEATABLE READ"), ([], []))

    def test_each_violation_detected(self):
        for key in ("neg_stock", "stock_mismatch", "account_mismatch", "orders_without_receipt",
                    "transfers_without_receipt", "receipts_without_effect", "orders_without_items",
                    "items_without_order", "bad_totals"):
            f = _clean_facts()
            f[key] = 1
            v, _ = I.check(f, self.LED, "REPEATABLE READ")
            self.assertTrue(any(key in x for x in v), key)
        f = _clean_facts()
        f["balance_total"] -= 1
        self.assertTrue(I.check(f, self.LED, "REPEATABLE READ")[0])

    def test_lost_and_unexpected_commits(self):
        self.assertTrue(any("lost commit" in x for x in I.check(_clean_facts(orders=9), self.LED,
                                                                 "REPEATABLE READ")[0]))
        self.assertTrue(any("unexpected" in x for x in I.check(_clean_facts(orders=11), self.LED,
                                                                "REPEATABLE READ")[0]))
        led = {"order": {"committed": 10, "ambiguous": 1}, "transfer": {"committed": 5, "ambiguous": 0}}
        self.assertEqual(I.check(_clean_facts(orders=11), led, "REPEATABLE READ"), ([], []))

    def test_negative_balance_is_observation_only_under_rc(self):
        f = _clean_facts()
        f["neg_balance"] = 2
        self.assertEqual(I.check(f, self.LED, "READ COMMITTED")[0], [])
        self.assertTrue(I.check(f, self.LED, "READ COMMITTED")[1])
        self.assertTrue(I.check(f, self.LED, "REPEATABLE READ")[0])


import scenarios as SC  # noqa: E402


class Scenarios(unittest.TestCase):
    def test_expected_table_complete(self):
        for s in SC.SCENARIOS:
            for lvl in W.LEVELS:
                self.assertIn((s, lvl), SC.EXPECTED)

    def test_outcome_and_judge(self):
        self.assertEqual(SC.outcome_of(True, ["40001"], True), "anomaly")
        self.assertEqual(SC.outcome_of(False, ["40001"], True), "prevented_error")
        self.assertEqual(SC.outcome_of(False, [], True), "prevented_wait")
        self.assertEqual(SC.outcome_of(False, [], False), "no_anomaly")
        self.assertEqual(SC.judge("lost_update", "READ COMMITTED", "anomaly"), "as_expected")
        self.assertEqual(SC.judge("lost_update", "REPEATABLE READ", "anomaly"), "violation")
        self.assertEqual(SC.judge("write_skew", "REPEATABLE READ", "prevented_error"), "differs_no_violation")
        self.assertEqual(SC.judge("deadlock", "READ COMMITTED", "not_applicable"), "not_applicable")
        self.assertEqual(SC.judge("deadlock", "READ COMMITTED", "inconclusive"), "inconclusive")


if __name__ == "__main__":
    unittest.main()
