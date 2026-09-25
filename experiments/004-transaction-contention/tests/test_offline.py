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


if __name__ == "__main__":
    unittest.main()
