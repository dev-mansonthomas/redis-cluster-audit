"""Recommendation detections.

Phase A (characterization, existing detections): C1, C7, C8, C9, C10.
Phase B (new detections, red->green): N1 noeviction, N2 app-as-default,
N3 per-IP concentration, N4 monolithic strings, N5 fragmentation,
N6 latency, N7 AOF+RDB, N8 hot key.
"""

import audit


def _baseline():
    """Inputs that produce NO recommendations; each test perturbs one field."""
    all_node_data = [{
        "label": "n:6379",
        "config": {"timeout": "300", "maxmemory": "1073741824",
                   "maxmemory-policy": "allkeys-lru",
                   "slowlog-log-slower-than": "10000",
                   "latency-monitor-threshold": "100"},
        "info": {"connected_slaves": 1, "role": "master", "mem_fragmentation_ratio": 1.0},
        "slowlog": [], "latency": [],
    }]
    conn_analysis = {
        "idle_buckets": {"<10s": 0, "10s-1min": 0, "1min-10min": 0, ">10min": 0},
        "per_ip": {}, "cmd_dist": {}, "total": 0, "default_user_external": 0,
    }
    keyspace = {"ttl_distribution": {"no_ttl": 0, "has_ttl": 10},
                "type_distribution": {"string": 5, "hash": 5},
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


# ── Phase A — existing detections ─────────────────────────────────────────────

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


# ── Phase B — new detections ──────────────────────────────────────────────────

def test_noeviction_on_capped_cache_flagged():          # N1
    nd, conn, ks, sec, stats = _baseline()
    nd[0]["config"]["maxmemory-policy"] = "noeviction"   # maxmemory already capped
    assert any("noeviction" in t.lower() for t in _titles(nd, conn, ks, sec, stats))


def test_app_as_default_user_flagged():                 # N2
    nd, conn, ks, sec, stats = _baseline()
    conn["default_user_external"] = 3
    titles = _titles(nd, conn, ks, sec, stats)
    assert any("default" in t.lower() and "user" in t.lower() for t in titles)


def test_connection_concentration_flagged():            # N3
    nd, conn, ks, sec, stats = _baseline()
    conn["per_ip"] = {"10.0.0.9": 500}
    assert any("connections from" in t.lower() for t in _titles(nd, conn, ks, sec, stats))


def test_monolithic_string_model_flagged():             # N4
    nd, conn, ks, sec, stats = _baseline()
    ks["total_scanned"] = 100
    ks["type_distribution"] = {"string": 95, "hash": 5}
    ks["big_keys"] = [{"key": "blob", "type": "string", "size_bytes": 200_000, "ttl": -1}]
    assert any("monolithic string" in t.lower() for t in _titles(nd, conn, ks, sec, stats))


def test_high_fragmentation_flagged():                  # N5
    nd, conn, ks, sec, stats = _baseline()
    nd[0]["info"]["mem_fragmentation_ratio"] = 2.0
    assert any("fragmentation" in t.lower() for t in _titles(nd, conn, ks, sec, stats))


def test_latency_monitor_disabled_flagged():            # N6
    nd, conn, ks, sec, stats = _baseline()
    nd[0]["config"]["latency-monitor-threshold"] = "0"
    assert any("latency monitor" in t.lower() for t in _titles(nd, conn, ks, sec, stats))


def test_latency_events_flagged():                      # N6
    nd, conn, ks, sec, stats = _baseline()
    nd[0]["latency"] = [{"event": "fork", "latest_ms": 120, "max_ms": 300}]
    assert any("latency spike" in t.lower() for t in _titles(nd, conn, ks, sec, stats))


def test_aof_and_rdb_both_flagged():                    # N7
    nd, conn, ks, sec, stats = _baseline()
    nd[0]["config"]["appendonly"] = "yes"
    nd[0]["config"]["save"] = "3600 1"
    assert any("aof" in t.lower() and "rdb" in t.lower() for t in _titles(nd, conn, ks, sec, stats))


def test_hot_key_from_slowlog_flagged():                # N8
    nd, conn, ks, sec, stats = _baseline()
    nd[0]["slowlog"] = ([{"command": "GET hot:key:1"} for _ in range(8)]
                        + [{"command": "GET other:key"}])
    assert any("hot key" in t.lower() for t in _titles(nd, conn, ks, sec, stats))
