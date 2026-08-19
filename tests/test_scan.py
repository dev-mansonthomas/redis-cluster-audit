"""Task 5 & 8 — keyspace scan: pipelined, samples!=0, skips vanished keys."""

import audit
from tests.conftest import FakeNode

DATASET = {
    "k:str:big": {"type": "string", "ttl": -1, "mem": 100000},
    "k:str:ttl": {"type": "string", "ttl": 1800, "mem": 500},
    "k:hash":    {"type": "hash",   "ttl": 7200, "mem": 2000},
}


def test_scan_distributions_golden():
    node = FakeNode(meta=DATASET)
    res = audit.scan_keyspace([node])
    assert res["total_scanned"] == 3
    assert res["type_distribution"] == {"string": 2, "hash": 1}
    assert res["ttl_distribution"].get("no_ttl") == 1
    assert res["ttl_distribution"].get("has_ttl") == 2
    # big_keys sorted by size desc
    assert [k["key"] for k in res["big_keys"]] == ["k:str:big", "k:hash", "k:str:ttl"]
    # only the sub-hour TTL key is "expiring soon"
    assert [k["key"] for k in res["expiring_soon"]] == ["k:str:ttl"]


def test_scan_is_pipelined_with_nonzero_samples():
    node = FakeNode(meta=DATASET)
    audit.scan_keyspace([node])
    assert node.pipeline_executes >= 1          # batched, not per-key round-trips
    assert node.direct_type_calls == 0          # no unbatched TYPE calls
    assert node.memory_samples                  # MEMORY USAGE was issued
    assert 0 not in node.memory_samples         # never the exact O(N) samples=0
    assert all(s and s > 0 for s in node.memory_samples)


def test_scan_skips_keys_that_vanished_between_scan_and_read():
    node = FakeNode(meta=DATASET, scan_keys=list(DATASET) + ["k:gone"])
    res = audit.scan_keyspace([node])
    assert res["total_scanned"] == 3            # ghost not counted
    assert "none" not in res["type_distribution"]
    assert all(k["type"] != "none" for k in res["big_keys"])
