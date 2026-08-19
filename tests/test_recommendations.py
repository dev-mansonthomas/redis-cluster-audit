"""Phase A coverage — the perf/config recommendations the Docker fixture never
triggered (C1, C7, C8, C9, C10). Characterization tests: they lock that each
existing detection path fires."""

import audit


def _baseline():
    """Inputs that produce NO recommendations; each test perturbs one field."""
    all_node_data = [{
        "label": "n:6379",
        "config": {"timeout": "300", "maxmemory": "1gb",
                   "slowlog-log-slower-than": "10000"},
        "info": {"connected_slaves": 1, "role": "master"},
    }]
    conn_analysis = {
        "idle_buckets": {"<10s": 0, "10s-1min": 0, "1min-10min": 0, ">10min": 0},
        "per_ip": {}, "cmd_dist": {}, "total": 0,
    }
    keyspace = {"ttl_distribution": {"no_ttl": 0, "has_ttl": 10},
                "total_scanned": 10, "big_keys": []}
    security = {"findings": []}
    stats = {"hit_ratio": 0.99, "total_evictions": 0}
    return all_node_data, conn_analysis, keyspace, security, stats


def _titles(all_node_data, conn_analysis, keyspace, security, stats):
    recs = audit.generate_recommendations(all_node_data, conn_analysis,
                                           keyspace, security, stats)
    return [r["title"] for r in recs]


def test_baseline_has_no_recommendations():
    assert _titles(*_baseline()) == []


def test_maxmemory_zero_flagged():                      # C1
    nd, conn, ks, sec, stats = _baseline()
    nd[0]["config"]["maxmemory"] = "0"
    assert any("maxmemory=0" in t for t in _titles(nd, conn, ks, sec, stats))


def test_evictions_flagged():                           # C7
    nd, conn, ks, sec, stats = _baseline()
    stats["total_evictions"] = 1234
    assert any("evicted" in t for t in _titles(nd, conn, ks, sec, stats))


def test_low_hit_ratio_flagged():                       # C8
    nd, conn, ks, sec, stats = _baseline()
    stats["hit_ratio"] = 0.50
    assert any("hit ratio is low" in t.lower() for t in _titles(nd, conn, ks, sec, stats))


def test_idle_connections_flagged():                    # C9
    nd, conn, ks, sec, stats = _baseline()
    conn["idle_buckets"][">10min"] = 50
    assert any("idle > 10 minutes" in t for t in _titles(nd, conn, ks, sec, stats))


def test_permissive_slowlog_threshold_flagged():        # C10
    nd, conn, ks, sec, stats = _baseline()
    nd[0]["config"]["slowlog-log-slower-than"] = "200000"
    assert any("too permissive" in t for t in _titles(nd, conn, ks, sec, stats))
