"""Shared test fixtures — fake Redis clients so unit tests need no live server."""

import pytest


class _FakePipeline:
    """Records queued commands; execute() returns results in order."""

    def __init__(self, node):
        self._node = node
        self._ops = []

    def type(self, key):
        self._ops.append(("type", key))
        return self

    def ttl(self, key):
        self._ops.append(("ttl", key))
        return self

    def memory_usage(self, key, samples=None):
        self._node.memory_samples.append(samples)
        self._ops.append(("memory_usage", key))
        return self

    def execute(self):
        self._node.pipeline_executes += 1
        out = [self._node._value(op, key) for (op, key) in self._ops]
        self._ops = []
        return out


class FakeNode:
    """
    Minimal stand-in for a redis.Redis node connection.

    meta: {key: {"type": str, "ttl": int, "mem": int|None}}
    scan_keys: keys returned by SCAN (defaults to meta's keys). Include a key
    here but NOT in meta to simulate a key that expired between SCAN and read.
    """

    def __init__(self, meta=None, scan_keys=None, info=None, config=None,
                 slowlog=None, clients=None, label="fake:6379"):
        self.meta = meta or {}
        self.scan_keys = list(scan_keys) if scan_keys is not None else list(self.meta)
        self._info = info or {}
        self._config = config or {}
        self._slowlog = slowlog or []
        self._clients = clients or []
        self.label = label
        # instrumentation
        self.memory_samples = []
        self.pipeline_executes = 0
        self.direct_type_calls = 0
        self.direct_ttl_calls = 0
        self.direct_memory_calls = 0

    def _value(self, op, key):
        m = self.meta.get(key)
        if m is None:  # vanished between SCAN and read
            return {"type": "none", "ttl": -2, "memory_usage": None}[op]
        return {"type": m["type"], "ttl": m["ttl"], "memory_usage": m["mem"]}[op]

    def scan(self, cursor=0, count=100):
        return (0, list(self.scan_keys))

    def pipeline(self, transaction=True):
        self.pipeline_transaction = transaction
        return _FakePipeline(self)

    # direct (non-pipelined) accessors — used by the pre-refactor code path
    def type(self, key):
        self.direct_type_calls += 1
        return self._value("type", key)

    def ttl(self, key):
        self.direct_ttl_calls += 1
        return self._value("ttl", key)

    def memory_usage(self, key, samples=None):
        self.direct_memory_calls += 1
        self.memory_samples.append(samples)
        return self._value("memory_usage", key)


@pytest.fixture
def make_user():
    """Build an ACL user dict shaped like audit._parse_acl_entry output."""

    def _make(name, enabled=True, nopass=False, all_commands=False,
              all_keys=True, dangerous=False, write=False):
        return {
            "name": name,
            "enabled": enabled,
            "nopass": nopass,
            "has_password": (not nopass),
            "key_patterns": ["~*"] if all_keys else [],
            "channel_patterns": [],
            "commands": [],
            "has_all_keys": all_keys,
            "has_all_commands": all_commands,
            "has_dangerous": dangerous or all_commands,
            "has_write": write or all_commands,
            "raw": name,
        }

    return _make


@pytest.fixture
def secure_config():
    """A config dict with no network/persistence security findings."""
    return {
        "protected-mode": "yes",
        "bind": "127.0.0.1",
        "tls-port": "6379",
        "appendonly": "yes",
        "save": "3600 1",
    }
