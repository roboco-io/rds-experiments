"""Pure safety helpers for E001: identity, run prefix, manifest, cleanup selection.

This module never contacts AWS so it can be unit tested offline.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta, timezone

REGION = "ap-northeast-2"
PROFILE = "roboco"
CONFIGS = ("D1", "R1", "A1", "A2")  # provisioning order: DSQL first
TAG_PREFIX = "e001:run-prefix"
TAG_CONFIG = "e001:config"
TAG_EXPIRES = "e001:expires-at"
TAG_MANAGED = "e001:managed-by"
MANAGED_BY = "e001-sql-compatibility-harness"
MAX_LIFETIME_MIN = 240
# Dependents first; network last. Only these resource types are ever deleted.
DELETION_ORDER = (
    "dsql_cluster", "db_instance", "db_cluster", "db_subnet_group",
    "security_group", "subnet", "internet_gateway", "vpc",
)
_PREFIX_RE = re.compile(r"^e001-\d{8}t\d{6}z-[a-z0-9]{4}$")
_ACCOUNT_RE = re.compile(r"^\d{12}$")


class SafetyError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def new_run_prefix(now: datetime | None = None) -> str:
    now = now or utcnow()
    return f"e001-{now.strftime('%Y%m%dt%H%M%Sz')}-{secrets.token_hex(2)}"


def validate_run_prefix(prefix: str) -> str:
    if not _PREFIX_RE.match(prefix or ""):
        raise SafetyError(f"invalid run prefix: {prefix!r}")
    return prefix


def validate_account_id(account: str) -> str:
    if not _ACCOUNT_RE.match(account or ""):
        raise SafetyError("--account-id must be a 12-digit AWS account ID")
    return account


def assert_identity(expected_account: str, identity: dict) -> None:
    """identity is an STS GetCallerIdentity response."""
    validate_account_id(expected_account)
    actual = identity.get("Account")
    if actual != expected_account:
        raise SafetyError(f"STS account {actual!r} does not match confirmed account {expected_account!r}")


def validate_client_cidr(cidr: str) -> str:
    """Allow exactly one public IPv4 address (/32). Never 0.0.0.0/0 or ranges."""
    try:
        net = ipaddress.ip_network(cidr, strict=True)
    except ValueError as exc:
        raise SafetyError(f"invalid --client-cidr: {exc}") from None
    if net.version != 4 or net.prefixlen != 32:
        raise SafetyError("--client-cidr must be a single IPv4 /32")
    if not net.network_address.is_global:
        raise SafetyError("--client-cidr must be a public (global) IPv4 address")
    return str(net)


def resource_tags(prefix: str, config: str, expires_at: str) -> dict:
    return {
        TAG_PREFIX: prefix, TAG_CONFIG: config, TAG_EXPIRES: expires_at,
        TAG_MANAGED: MANAGED_BY, "Name": f"{prefix}-{config.lower()}",
    }


def tag_list(tags: dict) -> list[dict]:
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def tags_to_dict(tag_list_value) -> dict:
    return {t["Key"]: t["Value"] for t in (tag_list_value or [])}


def owned(tags: dict, prefix: str, config: str | None = None) -> bool:
    """True only if the resource carries this run's exact ownership tags."""
    if tags.get(TAG_PREFIX) != prefix or tags.get(TAG_MANAGED) != MANAGED_BY:
        return False
    return config is None or tags.get(TAG_CONFIG) == config


def write_private(path: str, obj) -> None:
    """Atomically write JSON with mode 0600 (directory 0700)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True, default=str)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.chmod(path, 0o600)


class Manifest:
    """Local, persisted inventory of every resource this run requested or created."""

    def __init__(self, path: str, data: dict):
        self.path = path
        self.data = data

    @classmethod
    def create(cls, path, account, region, prefix, lifetime_min, identity_arn, now=None):
        validate_account_id(account)
        validate_run_prefix(prefix)
        if not 1 <= lifetime_min <= MAX_LIFETIME_MIN:
            raise SafetyError(f"lifetime must be 1..{MAX_LIFETIME_MIN} minutes")
        if os.path.exists(path):
            raise SafetyError(f"manifest already exists: {path}")
        now = now or utcnow()
        m = cls(path, {
            "experiment": "E001", "account": account, "region": region, "prefix": prefix,
            "caller_arn": identity_arn, "created_at": iso(now),
            "expires_at": iso(now + timedelta(minutes=lifetime_min)),
            "resources": [], "config_status": {}, "connections": {}, "events": [],
        })
        m.save()
        return m

    @classmethod
    def load(cls, path):
        with open(path) as fh:
            data = json.load(fh)
        validate_run_prefix(data["prefix"])
        return cls(path, data)

    @property
    def prefix(self) -> str:
        return self.data["prefix"]

    def save(self) -> None:
        write_private(self.path, self.data)

    def assert_scope(self, account: str, region: str) -> None:
        if self.data["account"] != account or self.data["region"] != region:
            raise SafetyError("manifest account/region differ from the confirmed account/region")

    def expired(self, now=None) -> bool:
        return (now or utcnow()) >= parse_iso(self.data["expires_at"])

    def minutes_left(self, now=None) -> float:
        return (parse_iso(self.data["expires_at"]) - (now or utcnow())).total_seconds() / 60

    def event(self, config, name, **fields) -> None:
        self.data["events"].append({"ts": iso(utcnow()), "config": config, "event": name, **fields})
        self.save()

    def find(self, config, rtype, rid):
        for r in self.data["resources"]:
            if (r["config"], r["type"], r["id"]) == (config, rtype, rid):
                return r
        return None

    def add_resource(self, config, rtype, rid, state="requested", **extra) -> dict:
        if rtype not in DELETION_ORDER:
            raise SafetyError(f"unknown resource type {rtype}")
        r = self.find(config, rtype, rid)
        if r is None:
            r = {"config": config, "type": rtype, "id": rid, "state": state,
                 "recorded_at": iso(utcnow()), "extra": {}}
            self.data["resources"].append(r)
        r["state"] = state if r["state"] != "deleted" else r["state"]
        r["extra"].update(extra)
        self.save()
        return r

    def set_state(self, config, rtype, rid, state, **extra) -> None:
        r = self.find(config, rtype, rid)
        if r is None:
            raise SafetyError(f"resource not in manifest: {config}/{rtype}/{rid}")
        r["state"] = state
        r["extra"].update(extra)
        r[f"{state}_at"] = iso(utcnow())
        self.save()


def active_configs(data: dict) -> set[str]:
    return {r["config"] for r in data["resources"] if r["state"] != "deleted"}


def check_can_provision(data: dict, config: str, allow_order_override: bool = False) -> None:
    if config not in CONFIGS:
        raise SafetyError(f"unknown config {config}")
    others = active_configs(data) - {config}
    if others:
        raise SafetyError(f"one config at a time: clean up {sorted(others)} first")
    if any(r["config"] == config for r in data["resources"]):
        raise SafetyError(f"{config} already provisioned in this run prefix; start a new prefix")
    if not allow_order_override:
        for earlier in CONFIGS[: CONFIGS.index(config)]:
            if data["config_status"].get(earlier) != "cleaned":
                raise SafetyError(f"order D1->R1->A1->A2: {earlier} not cleaned yet "
                                  "(use --allow-order-override to record a deliberate deviation)")


def cleanup_plan(data: dict, config: str) -> list[dict]:
    """Undeleted manifest resources of exactly one config, in safe deletion order."""
    if config not in CONFIGS:
        raise SafetyError(f"unknown config {config}")
    selected = [r for r in data["resources"] if r["config"] == config and r["state"] != "deleted"]
    for r in selected:
        if r["type"] not in DELETION_ORDER:
            raise SafetyError(f"refusing unknown resource type {r['type']}")
    return sorted(selected, key=lambda r: DELETION_ORDER.index(r["type"]))
