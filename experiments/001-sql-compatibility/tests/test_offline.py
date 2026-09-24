"""Offline tests: no AWS or database access. Run: python3 -m unittest discover -s tests"""
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import safety as S  # noqa: E402
import sqlcases as Q  # noqa: E402

ACCT = "123456789012"
PFX = "e001-20260924t010203z-ab12"


class Classification(unittest.TestCase):
    def test_classes(self):
        c = lambda s, db=True: Q.classify_error(s, db_error=db)  # noqa: E731
        self.assertEqual(c("0A000"), "unsupported")
        self.assertEqual(c("42601"), "rejected")
        self.assertEqual(c("28P01"), "auth_error")
        self.assertEqual(c("08006"), "infra_error")
        self.assertEqual(c(None), "infra_error")
        self.assertEqual(c("40001"), "conflict")
        self.assertEqual(c("23505"), "constraint")
        self.assertEqual(c("57014"), "canceled")
        self.assertEqual(c("42P01", db=False), "harness_error")

    def test_outcomes_distinguish_syntax_semantic_infra(self):
        self.assertEqual(Q.case_outcome(None, True), ("pass", True, True))
        self.assertEqual(Q.case_outcome(None, False), ("semantic_mismatch", True, False))
        self.assertEqual(Q.case_outcome("unsupported", None), ("unsupported", False, None))
        self.assertEqual(Q.case_outcome("infra_error", None)[0], "inconclusive_infra")
        self.assertEqual(Q.case_outcome("auth_error", None)[0], "inconclusive_auth")

    def test_case_registry_unique_and_covers_scope(self):
        names = [c.name for c in Q.CASES]
        self.assertEqual(len(names), len(set(names)))
        features = {c.feature for c in Q.CASES}
        for f in ("PK/UNIQUE/CHECK", "FK", "sequence", "identity", "UPSERT", "JOIN/CTE/window", "JSONB", "index",
                  "temp table", "partition", "function", "trigger", "SELECT FOR UPDATE", "isolation",
                  "timeout", "cancel", "common order txn"):
            self.assertIn(f, features)
        for c in Q.CASES:  # every table a case creates must be dropped by that case
            for t in __import__("re").findall(r"CREATE TABLE (e001_\w+)", c.fn.__code__.co_consts.__repr__()):
                self.assertTrue(any(t in d for d in c.drops), (c.name, t))


class Invariants(unittest.TestCase):
    def test_lost_update(self):
        self.assertEqual(Q.lost_update_violations(100, 111, [1, 10]), [])
        self.assertEqual(Q.lost_update_violations(100, 110, [10]), [])
        self.assertTrue(Q.lost_update_violations(100, 101, [1, 10]))
        self.assertTrue(Q.lost_update_violations(100, 100, []))

    def test_order(self):
        init = {1: 5, 2: 5, 3: 1}
        self.assertEqual(Q.order_invariant_violations(init, {1: 0, 2: 3, 3: 0}, {1: 5, 2: 2, 3: 1}, 3, 3, 3), [])
        self.assertTrue(Q.order_invariant_violations(init, {1: -1, 2: 3, 3: 0}, {1: 6, 2: 2, 3: 1}, 3, 3, 3))
        self.assertTrue(Q.order_invariant_violations(init, {1: 0, 2: 3, 3: 0}, {1: 5, 2: 2, 3: 1}, 3, 2, 3))
        self.assertTrue(Q.order_invariant_violations(init, {1: 1, 2: 3, 3: 0}, {1: 5, 2: 2, 3: 1}, 3, 3, 3))


class Safeguards(unittest.TestCase):
    def test_identity(self):
        S.assert_identity(ACCT, {"Account": ACCT})
        with self.assertRaises(S.SafetyError):
            S.assert_identity(ACCT, {"Account": "999999999999"})
        with self.assertRaises(S.SafetyError):
            S.validate_account_id("12345")

    def test_cidr(self):
        self.assertEqual(S.validate_client_cidr("8.8.8.8/32"), "8.8.8.8/32")
        for bad in ("0.0.0.0/0", "8.8.8.0/24", "10.0.0.1/32", "::1/128", "nonsense"):
            with self.assertRaises(S.SafetyError):
                S.validate_client_cidr(bad)

    def test_prefix_and_ownership(self):
        S.validate_run_prefix(S.new_run_prefix())
        with self.assertRaises(S.SafetyError):
            S.validate_run_prefix("prod-db")
        tags = S.resource_tags(PFX, "R1", "x")
        self.assertTrue(S.owned(tags, PFX, "R1"))
        self.assertFalse(S.owned(tags, PFX, "A1"))
        self.assertFalse(S.owned({**tags, S.TAG_MANAGED: "other"}, PFX))
        self.assertFalse(S.owned({}, PFX))

    def test_manifest_mode_scope_expiry(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "run", "manifest.json")
            now = datetime(2026, 9, 24, tzinfo=timezone.utc)
            m = S.Manifest.create(path, ACCT, S.REGION, PFX, 60, "arn", now=now)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with self.assertRaises(S.SafetyError):
                S.Manifest.create(path, ACCT, S.REGION, PFX, 60, "arn")
            with self.assertRaises(S.SafetyError):
                m.assert_scope("999999999999", S.REGION)
            self.assertFalse(m.expired(now + timedelta(minutes=59)))
            self.assertTrue(m.expired(now + timedelta(minutes=60)))
            m.add_resource("D1", "dsql_cluster", "abc", state="created")
            self.assertEqual(S.Manifest.load(path).data["resources"][0]["id"], "abc")
            with self.assertRaises(S.SafetyError):
                S.Manifest.create(os.path.join(d, "x.json"), ACCT, S.REGION, PFX, 10_000, "arn")


class CleanupSelection(unittest.TestCase):
    def data(self):
        rs = [("R1", "vpc", "vpc-1"), ("R1", "db_instance", "i1"), ("R1", "subnet", "s1"),
              ("R1", "security_group", "sg1"), ("R1", "db_subnet_group", "g1"), ("R1", "internet_gateway", "igw1"),
              ("A1", "db_cluster", "c1")]
        return {"resources": [{"config": c, "type": t, "id": i, "state": "created", "extra": {}} for c, t, i in rs]
                + [{"config": "R1", "type": "subnet", "id": "s0", "state": "deleted", "extra": {}}],
                "config_status": {}}

    def test_order_scope_and_skip_deleted(self):
        plan = S.cleanup_plan(self.data(), "R1")
        self.assertEqual([r["type"] for r in plan], ["db_instance", "db_subnet_group", "security_group", "subnet",
                                                     "internet_gateway", "vpc"])
        self.assertNotIn("c1", [r["id"] for r in plan])
        self.assertNotIn("s0", [r["id"] for r in plan])

    def test_unknown_type_refused(self):
        d = self.data()
        d["resources"].append({"config": "R1", "type": "ec2_instance", "id": "i-x", "state": "created"})
        with self.assertRaises(S.SafetyError):
            S.cleanup_plan(d, "R1")

    def test_one_config_at_a_time_and_order(self):
        d = self.data()
        with self.assertRaises(S.SafetyError):
            S.check_can_provision(d, "A2")  # R1/A1 still active
        empty = {"resources": [], "config_status": {}}
        S.check_can_provision(empty, "D1")
        with self.assertRaises(S.SafetyError):
            S.check_can_provision(empty, "R1")  # D1 must be cleaned first
        S.check_can_provision(empty, "R1", allow_order_override=True)
        S.check_can_provision({"resources": [], "config_status": {"D1": "cleaned"}}, "R1")


class FakeDsql:
    def __init__(self, pages, clusters):
        self.pages, self.clusters, self.deleted = pages, clusters, []

    def get_paginator(self, name):
        assert name == "list_clusters"
        return type("P", (), {"paginate": lambda _s: iter(self.pages)})()

    def get_cluster(self, identifier):
        c = self.clusters[identifier]
        return {"identifier": identifier, "arn": f"arn:dsql/{identifier}", "status": c["status"]}

    def list_tags_for_resource(self, resourceArn):
        return {"tags": self.clusters[resourceArn.split("/")[-1]]["tags"]}

    def delete_cluster(self, identifier):
        self.deleted.append(identifier)


class FakeEmpty:
    def __getattr__(self, name):
        keys = {"describe_vpcs": "Vpcs", "describe_subnets": "Subnets", "describe_security_groups": "SecurityGroups",
                "describe_internet_gateways": "InternetGateways", "get_resources": "ResourceTagMappingList"}
        return lambda **kw: {keys[name]: [{"ResourceARN": arn} for arn in self.stale] if name == "get_resources" else []}
    stale = ()


class FakeSession:
    def __init__(self, dsql, other):
        self.dsql, self.other = dsql, other

    def client(self, name):
        return self.dsql if name == "dsql" else self.other


class DsqlOrphans(unittest.TestCase):
    def setUp(self):
        import e001 as E
        self.E = E
        mine, other_run = S.resource_tags(PFX, "D1", "x"), S.resource_tags("e001-20260101t000000z-zz99", "D1", "x")
        self.fake = FakeDsql(
            [{"clusters": [{"identifier": "mine"}, {"identifier": "foreign"}]},
             {"clusters": [{"identifier": "other-run"}, {"identifier": "wrong-cfg"}, {"identifier": "gone"},
                           {"identifier": "unmanaged"}]}],
            {"mine": {"status": "ACTIVE", "tags": mine},
             "foreign": {"status": "ACTIVE", "tags": {"team": "prod"}},
             "other-run": {"status": "ACTIVE", "tags": other_run},
             "wrong-cfg": {"status": "ACTIVE", "tags": S.resource_tags(PFX, "R1", "x")},
             "gone": {"status": "DELETED", "tags": mine},
             "unmanaged": {"status": "ACTIVE", "tags": {**mine, S.TAG_MANAGED: "someone-else"}}})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = S.Manifest.create(os.path.join(self.tmp.name, "manifest.json"), ACCT, S.REGION, PFX, 60, "arn")
        self.sess = FakeSession(self.fake, FakeEmpty())

    def test_adopts_only_exact_owned_live_cluster(self):
        self.E.adopt_tagged_orphans(self.sess, self.m, "D1")
        ids = [(r["type"], r["id"]) for r in self.m.data["resources"]]
        self.assertEqual(ids, [("dsql_cluster", "mine")])
        self.assertTrue(self.m.data["resources"][0]["extra"]["adopted"])

    def test_adoption_only_for_d1(self):
        self.E.adopt_tagged_orphans(self.sess, self.m, "R1")
        self.assertEqual(self.m.data["resources"], [])

    def test_verify_counts_unrecorded_owned_cluster_not_stale_tag_index(self):
        FakeEmpty.stale = ("arn:dsql/gone",)
        self.addCleanup(setattr, FakeEmpty, "stale", ())
        orig = self.E.ART
        self.E.ART = self.tmp.name
        self.addCleanup(setattr, self.E, "ART", orig)
        rep = self.E.verify(self.sess, self.m)
        # Verification covers the entire run, including a resource tagged for
        # another config; only configuration-scoped adoption excludes it.
        self.assertEqual([c["id"] for c in rep["dsql_unrecorded_live"]], ["mine", "wrong-cfg"])
        self.assertEqual(rep["remaining_count"], 2)
        self.assertEqual(rep["tag_index_arns_for_review"], ["arn:dsql/gone"])
        self.fake.clusters["mine"]["status"] = "DELETED"
        self.fake.clusters["wrong-cfg"]["status"] = "DELETED"
        self.assertEqual(self.E.verify(self.sess, self.m)["remaining_count"], 0)
        self.assertEqual(self.fake.deleted, [])


if __name__ == "__main__":
    unittest.main()
