#!/usr/bin/env python3
"""E001 harness CLI: init / discover / provision / run / cleanup / verify / cycle / summarize.

Every AWS command requires --account-id and verifies it against STS before acting.
Only resources recorded in this run's manifest AND carrying this run's tags are deleted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time

import safety as S

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "artifacts")
DEVIATION = {
    "D1": "단일 리전 클러스터(계획과 동일). 로컬 클라이언트 기능 검사 전용",
    "R1": "R1-lite: 소형 클래스·gp3 20GiB·기본 Single-AZ(--multi-az 시 standby) — 계획 r6g.xlarge Multi-AZ와 다름",
    "A1": "A1-lite: 소형 클래스 writer 1대, reader 없음 — 계획 r6g.xlarge writer+reader와 다름",
    "A2": "A2-lite: Serverless v2 writer 1대 0.5–2 ACU, reader 없음 — 계획 4–32 ACU writer+reader와 다름",
}
CLASS_CANDIDATES = {"R1": ["db.t4g.micro", "db.t4g.small", "db.m7g.large"],
                    "A1": ["db.t4g.medium", "db.r7g.large", "db.r6g.large"], "A2": ["db.serverless"]}
ENGINE = {"R1": "postgres", "A1": "aurora-postgresql", "A2": "aurora-postgresql"}
NOT_FOUND = {"ResourceNotFoundException", "DBInstanceNotFound", "DBInstanceNotFoundFault", "DBClusterNotFoundFault",
             "DBSubnetGroupNotFoundFault", "InvalidGroup.NotFound", "InvalidSubnetID.NotFound",
             "InvalidInternetGatewayID.NotFound", "InvalidVpcID.NotFound", "DBSnapshotNotFound",
             "DBClusterSnapshotNotFoundFault", "DBInstanceAutomatedBackupNotFound",
             "DBClusterAutomatedBackupNotFoundFault"}


def log(msg):
    print(f"[{S.iso(S.utcnow())}] {msg}", flush=True)


def run_dir(prefix):
    return os.path.join(ART, S.validate_run_prefix(prefix))


def code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def wait_until(fn, what, timeout_s, interval=15):
    deadline = time.monotonic() + timeout_s
    while True:
        result = fn()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        time.sleep(interval)


def session(args):
    import boto3
    S.validate_account_id(args.account_id)
    sess = boto3.Session(profile_name=args.profile, region_name=args.region)
    ident = sess.client("sts").get_caller_identity()
    S.assert_identity(args.account_id, ident)
    log(f"STS identity verified for confirmed account (region {args.region})")
    return sess, ident


def load_manifest(args):
    m = S.Manifest.load(os.path.join(run_dir(args.prefix), "manifest.json"))
    m.assert_scope(args.account_id, args.region)
    return m


# ------------------------------------------------------------ discovery ----

def _vkey(v):
    return tuple(int(x) for x in v.split("."))


def discover(sess, multi_az=False):
    rds = sess.client("rds")
    versions = {}
    for engine in set(ENGINE.values()):
        vs = set()
        for page in rds.get_paginator("describe_db_engine_versions").paginate(Engine=engine):
            for v in page["DBEngineVersions"]:
                if re.fullmatch(r"16\.\d+", v["EngineVersion"]) and v.get("Status", "available") == "available":
                    vs.add(v["EngineVersion"])
        versions[engine] = sorted(vs, key=_vkey, reverse=True)
    common = sorted(set(versions["postgres"]) & set(versions["aurora-postgresql"]), key=_vkey, reverse=True)
    out = {"discovered_at": S.iso(S.utcnow()), "versions": versions, "common_16": common, "choice": {}}
    for cfg, engine in ENGINE.items():
        ordered = common + [v for v in versions[engine] if v not in common]
        for ver in ordered:
            chosen = None
            for cls in CLASS_CANDIDATES[cfg]:
                opts = rds.describe_orderable_db_instance_options(
                    Engine=engine, EngineVersion=ver, DBInstanceClass=cls, Vpc=True)["OrderableDBInstanceOptions"]
                if cfg == "R1":
                    opts = [o for o in opts if o.get("StorageType") == "gp3" and (o.get("MultiAZCapable") or not multi_az)]
                if opts:
                    chosen = {"engine": engine, "version": ver, "class": cls, "common_minor": ver in common,
                              "azs": sorted({az["Name"] for o in opts for az in o.get("AvailabilityZones", [])})}
                    break
            if chosen:
                out["choice"][cfg] = chosen
                break
        if cfg not in out["choice"]:
            out["choice"][cfg] = {"error": "no orderable PG16 version/class among candidates"}
    return out


# ----------------------------------------------------------- provisioning --

def provision(sess, m, cfg, args):
    S.check_can_provision(m.data, cfg, args.allow_order_override)
    if m.minutes_left() < (15 if cfg == "D1" else 60):
        raise S.SafetyError("not enough absolute lifetime left for this config; start a new prefix")
    tags = S.resource_tags(m.prefix, cfg, m.data["expires_at"])
    m.data["config_status"][cfg] = "provisioning"
    m.event(cfg, "provision_start", deviation=DEVIATION[cfg])
    if cfg == "D1":
        dsql = sess.client("dsql")
        r = dsql.create_cluster(deletionProtectionEnabled=False, tags=tags, clientToken=f"{m.prefix}-d1")
        m.add_resource(cfg, "dsql_cluster", r["identifier"], state="created", arn=r["arn"])
        cid = r["identifier"]
        got = wait_until(lambda: (lambda c: c if c["status"] in ("ACTIVE", "IDLE") else None)(
            dsql.get_cluster(identifier=cid)), "DSQL ACTIVE", 1800)
        endpoint = got.get("endpoint") or f"{cid}.dsql.{m.data['region']}.on.aws"
        m.data["connections"][cfg] = {"host": endpoint, "user": "admin", "dbname": "postgres"}
    else:
        disc_path = os.path.join(run_dir(m.prefix), "discovery.json")
        if not os.path.exists(disc_path):
            S.write_private(disc_path, discover(sess, args.multi_az))
        with open(disc_path) as fh:
            choice = json.load(fh)["choice"][cfg]
        if "error" in choice:
            raise S.SafetyError(f"{cfg}: {choice['error']}")
        net = provision_network(sess, m, cfg, S.validate_client_cidr(args.client_cidr), tags, choice["azs"])
        provision_rds(sess, m, cfg, choice, net, tags, args.multi_az)
    m.data["config_status"][cfg] = "provisioned"
    m.event(cfg, "provision_done")


def provision_network(sess, m, cfg, cidr, tags, azs):
    ec2, rds = sess.client("ec2"), sess.client("rds")

    def spec(rt):
        return [{"ResourceType": rt, "Tags": S.tag_list(tags)}]
    vpc = ec2.create_vpc(CidrBlock="10.91.0.0/16", TagSpecifications=spec("vpc"))["Vpc"]["VpcId"]
    m.add_resource(cfg, "vpc", vpc, state="created")
    ec2.get_waiter("vpc_available").wait(VpcIds=[vpc])
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsHostnames={"Value": True})
    zones = sorted(z["ZoneName"] for z in ec2.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}, {"Name": "zone-type", "Values": ["availability-zone"]}]
    )["AvailabilityZones"] if not azs or z["ZoneName"] in azs)[:2]
    if len(zones) < 2:
        raise S.SafetyError("need two AZs for a DB subnet group")
    subnets = []
    for i, az in enumerate(zones):
        sid = ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.91.{i}.0/24", AvailabilityZone=az,
                                TagSpecifications=spec("subnet"))["Subnet"]["SubnetId"]
        m.add_resource(cfg, "subnet", sid, state="created", az=az)
        subnets.append(sid)
    igw = ec2.create_internet_gateway(TagSpecifications=spec("internet-gateway"))["InternetGateway"]["InternetGatewayId"]
    m.add_resource(cfg, "internet_gateway", igw, state="created", vpc=vpc)
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
    rt = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc]},
                                            {"Name": "association.main", "Values": ["true"]}])["RouteTables"][0]
    ec2.create_route(RouteTableId=rt["RouteTableId"], DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)  # egress path
    name = f"{m.prefix}-{cfg.lower()}"
    sg = ec2.create_security_group(GroupName=name, Description="E001 PostgreSQL from one client /32 only",
                                   VpcId=vpc, TagSpecifications=spec("security-group"))["GroupId"]
    m.add_resource(cfg, "security_group", sg, state="created")
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
        "IpRanges": [{"CidrIp": cidr, "Description": "E001 operator client"}]}])
    m.add_resource(cfg, "db_subnet_group", name, state="requested")
    rds.create_db_subnet_group(DBSubnetGroupName=name, DBSubnetGroupDescription="E001 temporary",
                               SubnetIds=subnets, Tags=S.tag_list(tags))
    m.set_state(cfg, "db_subnet_group", name, "created")
    return {"sg": sg, "subnet_group": name}


def provision_rds(sess, m, cfg, choice, net, tags, multi_az):
    rds = sess.client("rds")
    password = secrets.token_urlsafe(24)
    S.write_private(os.path.join(run_dir(m.prefix), "secrets", f"{cfg}.json"), {"password": password})
    common = dict(Engine=choice["engine"], EngineVersion=choice["version"], MasterUsername="e001admin",
                  MasterUserPassword=password, DBSubnetGroupName=net["subnet_group"],
                  VpcSecurityGroupIds=[net["sg"]], DeletionProtection=False, StorageEncrypted=True,
                  CopyTagsToSnapshot=False, Tags=S.tag_list(tags))
    inst = dict(PubliclyAccessible=True, EnablePerformanceInsights=False, MonitoringInterval=0,
                AutoMinorVersionUpgrade=False)
    if cfg == "R1":
        iid = f"{m.prefix}-r1"
        m.add_resource(cfg, "db_instance", iid, state="requested", cluster_member=False)
        rds.create_db_instance(DBInstanceIdentifier=iid, DBInstanceClass=choice["class"], DBName="e001",
                               AllocatedStorage=20, StorageType="gp3", MultiAZ=bool(multi_az),
                               BackupRetentionPeriod=0, **common, **inst)
        m.set_state(cfg, "db_instance", iid, "created")
        host = wait_available_instance(rds, iid)
    else:
        cid, iid = f"{m.prefix}-{cfg.lower()}", f"{m.prefix}-{cfg.lower()}-w1"
        extra = {"ServerlessV2ScalingConfiguration": {"MinCapacity": 0.5, "MaxCapacity": 2}} if cfg == "A2" else {}
        m.add_resource(cfg, "db_cluster", cid, state="requested")
        rds.create_db_cluster(DBClusterIdentifier=cid, DatabaseName="e001", BackupRetentionPeriod=1,
                              StorageType="aurora", **common, **extra)
        m.set_state(cfg, "db_cluster", cid, "created")
        m.add_resource(cfg, "db_instance", iid, state="requested", cluster_member=True)
        rds.create_db_instance(DBInstanceIdentifier=iid, DBClusterIdentifier=cid, Engine=choice["engine"],
                               DBInstanceClass=choice["class"], Tags=S.tag_list(tags), **inst)
        m.set_state(cfg, "db_instance", iid, "created")
        wait_available_instance(rds, iid)
        host = rds.describe_db_clusters(DBClusterIdentifier=cid)["DBClusters"][0]["Endpoint"]
    m.data["connections"][cfg] = {"host": host, "user": "e001admin", "dbname": "e001",
                                  "engine_version": choice["version"], "class": choice["class"]}
    m.save()


def wait_available_instance(rds, iid):
    def ready():
        d = rds.describe_db_instances(DBInstanceIdentifier=iid)["DBInstances"][0]
        return d["Endpoint"]["Address"] if d["DBInstanceStatus"] == "available" and d.get("Endpoint") else None
    return wait_until(ready, f"{iid} available", 3600, 30)


# ------------------------------------------------------------- cleanup -----

def _probe(sess, r):
    """Return (exists, tags, detail) for one manifest resource."""
    t, rid = r["type"], r["id"]
    try:
        if t == "dsql_cluster":
            d = sess.client("dsql")
            c = d.get_cluster(identifier=rid)
            if c["status"] == "DELETED":
                return False, {}, c
            return True, d.list_tags_for_resource(resourceArn=c["arn"]).get("tags", {}), c
        if t == "db_instance":
            c = sess.client("rds").describe_db_instances(DBInstanceIdentifier=rid)["DBInstances"][0]
            return True, S.tags_to_dict(c.get("TagList")), c
        if t == "db_cluster":
            c = sess.client("rds").describe_db_clusters(DBClusterIdentifier=rid)["DBClusters"][0]
            return True, S.tags_to_dict(c.get("TagList")), c
        if t == "db_subnet_group":
            rds = sess.client("rds")
            c = rds.describe_db_subnet_groups(DBSubnetGroupName=rid)["DBSubnetGroups"][0]
            return True, S.tags_to_dict(rds.list_tags_for_resource(ResourceName=c["DBSubnetGroupArn"])["TagList"]), c
        ec2 = sess.client("ec2")
        call = {"security_group": ("describe_security_groups", "GroupIds", "SecurityGroups"),
                "subnet": ("describe_subnets", "SubnetIds", "Subnets"),
                "internet_gateway": ("describe_internet_gateways", "InternetGatewayIds", "InternetGateways"),
                "vpc": ("describe_vpcs", "VpcIds", "Vpcs")}[t]
        items = getattr(ec2, call[0])(**{call[1]: [rid]})[call[2]]
        return (True, S.tags_to_dict(items[0].get("Tags")), items[0]) if items else (False, {}, None)
    except Exception as exc:
        if code(exc) in NOT_FOUND:
            return False, {}, None
        raise


def _delete(sess, r, detail):
    t, rid = r["type"], r["id"]
    if t == "dsql_cluster":
        if detail.get("deletionProtectionEnabled"):
            raise S.SafetyError(f"{rid} has deletion protection; not modifying it automatically")
        if detail["status"] not in ("DELETING", "PENDING_DELETE"):
            sess.client("dsql").delete_cluster(identifier=rid)
    elif t == "db_instance":
        if detail["DBInstanceStatus"] != "deleting":
            kw = {} if r["extra"].get("cluster_member") else {"SkipFinalSnapshot": True, "DeleteAutomatedBackups": True}
            sess.client("rds").delete_db_instance(DBInstanceIdentifier=rid, **kw)
    elif t == "db_cluster":
        if detail["Status"] != "deleting":
            sess.client("rds").delete_db_cluster(DBClusterIdentifier=rid, SkipFinalSnapshot=True,
                                                 DeleteAutomatedBackups=True)
    elif t == "db_subnet_group":
        sess.client("rds").delete_db_subnet_group(DBSubnetGroupName=rid)
    elif t == "internet_gateway":
        ec2 = sess.client("ec2")
        for a in detail.get("Attachments", []):
            if a["VpcId"] == r["extra"].get("vpc"):
                ec2.detach_internet_gateway(InternetGatewayId=rid, VpcId=a["VpcId"])
        ec2.delete_internet_gateway(InternetGatewayId=rid)
    else:
        ec2 = sess.client("ec2")
        {"security_group": lambda: ec2.delete_security_group(GroupId=rid),
         "subnet": lambda: ec2.delete_subnet(SubnetId=rid),
         "vpc": lambda: ec2.delete_vpc(VpcId=rid)}[t]()


def owned_dsql_clusters(sess, prefix, cfg=None):
    """Live (non-DELETED) DSQL clusters whose own tags match this run (and config, if given)."""
    dsql, found = sess.client("dsql"), []
    for page in dsql.get_paginator("list_clusters").paginate():
        for item in page.get("clusters", []):
            try:
                c = dsql.get_cluster(identifier=item["identifier"])
                if c["status"] == "DELETED":
                    continue
                tags = dsql.list_tags_for_resource(resourceArn=c["arn"]).get("tags", {})
            except Exception as exc:
                if code(exc) in NOT_FOUND:
                    continue
                raise
            if S.owned(tags, prefix, cfg):
                found.append({"id": c["identifier"], "arn": c["arn"], "config": tags.get(S.TAG_CONFIG)})
    return found


def adopt_tagged_orphans(sess, m, cfg):
    """Record EC2/DSQL resources carrying this run+config tags that a crash left out of the manifest."""
    if cfg == "D1":
        for c in owned_dsql_clusters(sess, m.prefix, cfg):
            if not m.find(cfg, "dsql_cluster", c["id"]):
                m.add_resource(cfg, "dsql_cluster", c["id"], state="created", arn=c["arn"], adopted=True)
    ec2 = sess.client("ec2")
    flt = [{"Name": f"tag:{S.TAG_PREFIX}", "Values": [m.prefix]}, {"Name": f"tag:{S.TAG_CONFIG}", "Values": [cfg]}]
    for rtype, call, key, idk in (("vpc", "describe_vpcs", "Vpcs", "VpcId"),
                                  ("subnet", "describe_subnets", "Subnets", "SubnetId"),
                                  ("internet_gateway", "describe_internet_gateways", "InternetGateways", "InternetGatewayId"),
                                  ("security_group", "describe_security_groups", "SecurityGroups", "GroupId")):
        for item in getattr(ec2, call)(Filters=flt)[key]:
            if S.owned(S.tags_to_dict(item.get("Tags")), m.prefix, cfg) and not m.find(cfg, rtype, item[idk]):
                extra = {"vpc": item["Attachments"][0]["VpcId"]} if rtype == "internet_gateway" and item.get("Attachments") else {}
                m.add_resource(cfg, rtype, item[idk], state="created", adopted=True, **extra)


def cleanup(sess, m, cfg, timeout_s=3600):
    m.event(cfg, "cleanup_start")
    adopt_tagged_orphans(sess, m, cfg)
    failures = []
    for r in S.cleanup_plan(m.data, cfg):
        try:
            exists, tags, detail = _probe(sess, r)
            if exists:
                if not S.owned(tags, m.prefix, cfg):
                    raise S.SafetyError(f"{r['type']} {r['id']} lacks this run's tags; refusing to delete")
                log(f"deleting {cfg} {r['type']}")
                deadline = time.monotonic() + timeout_s
                while True:  # retry DependencyViolation while ENIs/dependents disappear
                    try:
                        _delete(sess, r, detail)
                        break
                    except Exception as exc:
                        if code(exc) in NOT_FOUND:
                            break
                        if code(exc) not in ("DependencyViolation", "InvalidDBSubnetGroupStateFault",
                                             "InvalidDBClusterStateFault", "InvalidDBInstanceState",
                                             "InvalidDBInstanceStateFault") or time.monotonic() > deadline:
                            raise
                        time.sleep(20)
                        exists, tags, detail = _probe(sess, r)
                        if not exists:
                            break
                wait_until(lambda: not _probe(sess, r)[0], f"{r['type']} gone", timeout_s, 20)
            m.set_state(cfg, r["type"], r["id"], "deleted")
        except Exception as exc:
            failures.append({"type": r["type"], "id": r["id"], "error": f"{type(exc).__name__}: {exc}"[:300]})
    m.data["config_status"][cfg] = "cleanup_failed" if failures else "cleaned"
    m.event(cfg, "cleanup_done", failures=failures)
    if failures:
        raise RuntimeError(f"cleanup incomplete for {cfg}: {failures}")


def verify(sess, m):
    rds = sess.client("rds")
    remaining, snapshots = [], []
    for r in m.data["resources"]:
        if _probe(sess, r)[0]:
            remaining.append({"config": r["config"], "type": r["type"], "id": r["id"]})
        checks = []
        if r["type"] == "db_instance":
            checks = [("describe_db_snapshots", {"DBInstanceIdentifier": r["id"]}, "DBSnapshots"),
                      ("describe_db_instance_automated_backups", {"DBInstanceIdentifier": r["id"]},
                       "DBInstanceAutomatedBackups")]
        elif r["type"] == "db_cluster":
            checks = [("describe_db_cluster_snapshots", {"DBClusterIdentifier": r["id"]}, "DBClusterSnapshots"),
                      ("describe_db_cluster_automated_backups", {"DBClusterIdentifier": r["id"]},
                       "DBClusterAutomatedBackups")]
        for call, kw, key in checks:
            try:
                n = len(getattr(rds, call)(**kw)[key])
            except Exception as exc:
                if code(exc) not in NOT_FOUND:
                    raise
                n = 0
            if n:
                snapshots.append({"id": r["id"], "call": call, "count": n})
    ec2 = sess.client("ec2")
    flt = [{"Name": f"tag:{S.TAG_PREFIX}", "Values": [m.prefix]}]
    tagged = {k: len(getattr(ec2, c)(Filters=flt)[k]) for c, k in
              (("describe_vpcs", "Vpcs"), ("describe_subnets", "Subnets"),
               ("describe_internet_gateways", "InternetGateways"), ("describe_security_groups", "SecurityGroups"))}
    in_manifest = {r["id"] for r in m.data["resources"] if r["type"] == "dsql_cluster"}
    dsql_unrecorded = [c for c in owned_dsql_clusters(sess, m.prefix) if c["id"] not in in_manifest]
    # Tag-index entries can be stale after deletion: listed for review only, never counted as live.
    tag_index = [x["ResourceARN"] for x in sess.client("resourcegroupstaggingapi").get_resources(
        TagFilters=[{"Key": S.TAG_PREFIX, "Values": [m.prefix]}])["ResourceTagMappingList"]]
    report = {"verified_at": S.iso(S.utcnow()), "prefix": m.prefix, "manifest_remaining": remaining,
              "snapshots_or_retained_backups": snapshots, "ec2_tag_scan": tagged,
              "dsql_unrecorded_live": dsql_unrecorded, "tag_index_arns_for_review": tag_index,
              "remaining_count": len(remaining) + sum(s["count"] for s in snapshots) + sum(tagged.values())
              + len(dsql_unrecorded)}
    S.write_private(os.path.join(run_dir(m.prefix), f"verify-{report['verified_at'].replace(':', '')}.json"), report)
    return report


# ------------------------------------------------------------------ run ----

def connector(sess, m, cfg, args):
    import psycopg
    conn = m.data["connections"][cfg]
    host = conn["host"]
    if cfg == "D1":
        dsql = sess.client("dsql")

        def connect():
            token = dsql.generate_db_connect_admin_auth_token(Hostname=host, Region=m.data["region"], ExpiresIn=900)
            return psycopg.connect(host=host, port=5432, user="admin", dbname="postgres", password=token,
                                   sslmode="verify-full", sslrootcert=args.dsql_sslrootcert,
                                   connect_timeout=15, autocommit=True)
        return connect, [host]
    if not args.rds_ca_bundle or not os.path.exists(args.rds_ca_bundle):
        raise S.SafetyError("--rds-ca-bundle must point to the downloaded RDS global CA bundle")
    with open(os.path.join(run_dir(m.prefix), "secrets", f"{cfg}.json")) as fh:
        password = json.load(fh)["password"]

    def connect():
        return psycopg.connect(host=host, port=5432, user=conn["user"], dbname=conn["dbname"], password=password,
                               sslmode="verify-full", sslrootcert=args.rds_ca_bundle,
                               connect_timeout=15, autocommit=True)
    return connect, [host, password]


def run_cases(sess, m, cfg, args):
    import platform
    import psycopg
    import sqlcases
    if m.expired():
        raise S.SafetyError("absolute lifetime exceeded; run cleanup instead")
    connect, hidden = connector(sess, m, cfg, args)

    def sanitize(text):
        for h in hidden:
            text = text.replace(h, "<redacted>")
        return text
    started = S.utcnow()
    run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-E001-{cfg}-r01"
    m.event(cfg, "run_start", run_id=run_id)
    env = sqlcases.capture_env(connect, sanitize)
    cases = []
    for c in sqlcases.CASES:
        if args.case and c.name not in args.case:
            continue
        if m.expired():
            raise S.SafetyError(f"absolute lifetime exceeded before case {c.name}; run cleanup instead")
        log(f"{cfg} case {c.name}")
        cases.append(sqlcases.run_case(c, connect, sanitize))
        log(f"  -> {cases[-1]['outcome']}")
    result = {"run_id": run_id, "experiment": "E001", "config": cfg, "deviation": DEVIATION[cfg],
              "started_at": S.iso(started), "ended_at": S.iso(S.utcnow()),
              "engine": {k: v for k, v in m.data["connections"][cfg].items() if k in ("engine_version", "class")},
              "client": {"python": platform.python_version(), "psycopg": psycopg.__version__,
                         "libpq": psycopg.pq.version(), "mode": "local client, functional only"},
              "env": env, "cases": cases, "unmeasured": sqlcases.UNMEASURED}
    S.write_private(os.path.join(run_dir(m.prefix), "results", f"{run_id}.json"), result)
    m.data["config_status"][cfg] = "ran"
    m.event(cfg, "run_done", run_id=run_id)
    return result


def summarize(prefix):
    rdir = os.path.join(run_dir(prefix), "results")
    runs = []
    for f in sorted(os.listdir(rdir)):
        with open(os.path.join(rdir, f)) as fh:
            runs.append(json.load(fh))
    cfgs = [r["config"] for r in runs]
    names = list(dict.fromkeys(c["case"] for r in runs for c in r["cases"]))
    table = {n: {r["config"]: next((f"{c['outcome']}" + (f" ({c['sqlstate']})" if c["sqlstate"] else "")
                                    for c in r["cases"] if c["case"] == n), "미실행") for r in runs} for n in names}
    lines = ["| case | " + " | ".join(cfgs) + " |", "| --- |" + " --- |" * len(cfgs)]
    lines += [f"| {n} | " + " | ".join(table[n][c] for c in cfgs) + " |" for n in names]
    out = {"run_ids": [r["run_id"] for r in runs], "matrix": table}
    S.write_private(os.path.join(run_dir(prefix), "summary.json"), out)
    with open(os.path.join(run_dir(prefix), "summary.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))


# ------------------------------------------------------------------ CLI ----

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["init", "discover", "provision", "run", "cleanup", "verify", "cycle", "summarize"])
    p.add_argument("--account-id", help="confirmed 12-digit AWS account ID (required for AWS commands)")
    p.add_argument("--profile", default=S.PROFILE)
    p.add_argument("--region", default=S.REGION)
    p.add_argument("--prefix", help="run prefix printed by init")
    p.add_argument("--config", choices=S.CONFIGS)
    p.add_argument("--client-cidr", help="your public IPv4 /32 (R1/A1/A2 only)")
    p.add_argument("--multi-az", action="store_true", help="R1 with standby (costs ~2x)")
    p.add_argument("--allow-order-override", action="store_true")
    p.add_argument("--max-lifetime-minutes", type=int, default=180)
    p.add_argument("--rds-ca-bundle")
    p.add_argument("--dsql-sslrootcert", default="system")
    p.add_argument("--case", action="append", help="limit to named case(s)")
    args = p.parse_args(argv)
    if args.region != S.REGION:
        raise S.SafetyError(f"E001 is fixed to {S.REGION}")
    if args.command == "summarize":
        return summarize(args.prefix)
    if not args.account_id:
        p.error("--account-id is required")
    sess, ident = session(args)
    if args.command == "init":
        prefix = S.new_run_prefix()
        S.Manifest.create(os.path.join(run_dir(prefix), "manifest.json"), args.account_id, args.region, prefix,
                          args.max_lifetime_minutes, ident["Arn"])
        print(prefix)
        return
    m = load_manifest(args)
    if args.command == "discover":
        S.write_private(os.path.join(run_dir(m.prefix), "discovery.json"), discover(sess, args.multi_az))
        log("discovery.json written")
    elif args.command == "verify":
        rep = verify(sess, m)
        log(f"remaining_count={rep['remaining_count']} (tag index ARNs for review: {len(rep['tag_index_arns_for_review'])})")
        sys.exit(0 if rep["remaining_count"] == 0 else 2)
    elif not args.config:
        p.error("--config is required")
    elif args.command == "provision":
        provision(sess, m, args.config, args)
    elif args.command == "run":
        run_cases(sess, m, args.config, args)
    elif args.command == "cleanup":
        cleanup(sess, m, args.config)
    elif args.command == "cycle":
        if args.config != "D1":
            S.validate_client_cidr(args.client_cidr or "")
        try:
            provision(sess, m, args.config, args)
            run_cases(sess, m, args.config, args)
        finally:
            cleanup_err = None
            try:  # always: an empty manifest may still hide tagged orphans to adopt
                cleanup(sess, m, args.config)
            except Exception as exc:
                cleanup_err = exc
                log(f"cleanup error: {type(exc).__name__}: {exc}"[:400])
            rep = verify(sess, m)
            log(f"remaining_count={rep['remaining_count']}")
            if rep["remaining_count"]:
                sys.exit(2)
            if cleanup_err:
                raise cleanup_err


if __name__ == "__main__":
    main()
