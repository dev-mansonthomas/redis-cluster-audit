"""
Redis Cluster Audit Script
==========================

Usage:
    pip install -r requirements.txt
    cp .env.example .env          # fill in your credentials
    python audit.py               # produces report.html in the current directory

What this script audits (requires a read-only user — the only write attempted
is a self-expiring canary in the permission check below, which a read-only user
rejects, so a correctly-provisioned audit makes no changes to production):
    1.  Permission check     — exits if user has write access, prints ACL creation script.
    2.  Cluster topology     — shard list, roles, Redis version.
    3.  Configuration        — maxclients, timeout, tcp-keepalive, maxmemory, eviction,
                               slowlog thresholds, persistence, TLS.
    4.  Memory               — used/peak/fragmentation per shard, MEMORY DOCTOR.
    5.  Performance stats    — hit ratio, evictions, latency (LATENCY LATEST), bandwidth.
    6.  Connections          — CLIENT LIST: per source IP, idle distribution, last command.
    7.  Slow log             — top slow commands across all shards.
    8.  Key space            — DBSIZE per shard, TTL distribution, type distribution
                               (SCAN sample — never uses KEYS *).
    9.  Big keys             — top N keys by memory usage (MEMORY USAGE).
    10. Security audit       — ACL users (over-privileged, nopass, default user),
                               network exposure (bind, protected-mode, TLS),
                               ACL LOG (recent auth failures / permission denials).
    11. Recommendations      — auto-generated, severity-ranked.
"""

import os
import sys
import time
import html
import argparse
from collections import defaultdict, Counter
from datetime import datetime

import redis
from redis.cluster import RedisCluster, ClusterNode
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

STARTUP_NODES = [
    ClusterNode(os.getenv("REDIS_HOST_1", "127.0.0.1"), int(os.getenv("REDIS_PORT_1", 6777))),
    ClusterNode(os.getenv("REDIS_HOST_2", "127.0.0.1"), int(os.getenv("REDIS_PORT_2", 6778))),
    ClusterNode(os.getenv("REDIS_HOST_3", "127.0.0.1"), int(os.getenv("REDIS_PORT_3", 6779))),
]
PASSWORD         = os.getenv("REDIS_PASSWORD") or None
USERNAME         = os.getenv("REDIS_USERNAME") or None
BIG_KEY_TOP_N    = int(os.getenv("BIG_KEY_TOP_N", 30))
SCAN_SAMPLE_SIZE = int(os.getenv("SCAN_SAMPLE_SIZE", 5000))
SLOWLOG_ENTRIES  = int(os.getenv("SLOWLOG_MAX_ENTRIES", 25))
# MEMORY USAGE sampling depth. 0 = examine every element (exact but O(N) on the
# server) — avoid on production; a small sample gives a good estimate cheaply.
SCAN_MEMORY_SAMPLES = int(os.getenv("SCAN_MEMORY_SAMPLES", 5))
# Warn when a single source IP holds more than this many connections (pool misconfig).
CONN_PER_IP_WARN = int(os.getenv("CONN_PER_IP_WARN", 200))

# LOCAL DOCKER ONLY — maps internal Docker bridge IPs back to 127.0.0.1:port
# so MOVED redirects work from the Mac host. Set REDIS_DOCKER_REMAP=false for production.
_DOCKER_ADDR_REMAP = {
    ("172.29.0.10", 6777): ("127.0.0.1", 6777),
    ("172.29.0.11", 6778): ("127.0.0.1", 6778),
    ("172.29.0.12", 6779): ("127.0.0.1", 6779),
}
_USE_DOCKER_REMAP = os.getenv("REDIS_DOCKER_REMAP", "false").lower() == "true"

OUTPUT_FILE = "report.html"

# ---------------------------------------------------------------------------
# ACL script shown when the user has write access
# ---------------------------------------------------------------------------

# Single source of truth for the read-only audit user's command grants.
# run.sh provisions the user from this exact list via `audit.py --print-acl`,
# so the printed customer script and the local test setup can never drift.
READONLY_ACL_COMMANDS = [
    "+INFO", "+CONFIG|GET",
    "+SLOWLOG|GET", "+SLOWLOG|LEN",
    "+MEMORY|USAGE", "+MEMORY|DOCTOR", "+MEMORY|STATS",
    "+CLIENT|LIST",
    "+DBSIZE",
    "+CLUSTER|INFO", "+CLUSTER|NODES", "+CLUSTER|SLOTS",
    "+CLUSTER|SHARDS", "+CLUSTER|KEYSLOT", "+CLUSTER|MYID",
    "+SCAN", "+TYPE", "+TTL", "+OBJECT|ENCODING", "+OBJECT|IDLETIME",
    "+ACL|LIST", "+ACL|USERS", "+ACL|CAT", "+ACL|LOG",
    "+LATENCY|LATEST", "+LATENCY|HISTORY",
    "+PING", "+READONLY", "+COMMAND",
]


def build_acl_setuser(user: str, password: str) -> str:
    """Build the `ACL SETUSER` line that grants exactly the read-only audit grants."""
    grants = " ".join(READONLY_ACL_COMMANDS)
    return f"ACL SETUSER {user} on >{password} ~* &* nocommands {grants}"


READONLY_ACL_SCRIPT = (
    "-- Run this on every Redis node as an admin user:\n\n"
    + build_acl_setuser("audit_ro", "{YOUR_PASSWORD}")
    + "\n\n-- Then set in your .env:\n"
    + "--   REDIS_USERNAME=audit_ro\n"
    + "--   REDIS_PASSWORD={YOUR_PASSWORD}"
)


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def connect_cluster() -> RedisCluster:
    kwargs = dict(
        startup_nodes=STARTUP_NODES,
        password=PASSWORD,
        decode_responses=True,
        # Tolerate partial slot coverage: an audit is often pointed at a
        # degraded cluster (a primary down / mid-resharding). This is the
        # real redis-py kwarg — skip_full_coverage_check is a legacy no-op.
        require_full_coverage=False,
        socket_timeout=10,
        socket_connect_timeout=5,
    )
    if USERNAME:
        kwargs["username"] = USERNAME
    if _USE_DOCKER_REMAP:
        kwargs["address_remap"] = lambda addr: _DOCKER_ADDR_REMAP.get(addr, addr)
    return RedisCluster(**kwargs)


def node_direct_connection(node) -> redis.Redis:
    """Direct (non-cluster) connection to a single node for admin commands."""
    kwargs = dict(
        host=node.host,
        port=node.port,
        password=PASSWORD,
        decode_responses=True,
        socket_timeout=10,
    )
    if USERNAME:
        kwargs["username"] = USERNAME
    return redis.Redis(**kwargs)


# ---------------------------------------------------------------------------
# 1. Permission check
# ---------------------------------------------------------------------------

def check_permissions(rc: RedisCluster):
    """
    Verify the connection is read-only by attempting a single throwaway write.

    With the recommended read-only ACL user the write is rejected (NOPERM) and
    nothing is ever written. If it unexpectedly succeeds, the user can modify
    data: the canary is removed immediately (UNLINK) and the audit aborts so it
    never runs with a write-capable account against production.
    """
    CANARY = "__audit_permission_canary__"
    try:
        rc.set(CANARY, "1", ex=5)
    except redis.exceptions.NoPermissionError:
        return
    except redis.exceptions.ResponseError as e:
        err = str(e).upper()
        if "NOPERM" in err or "READONLY" in err or "NOAUTH" in err:
            return
        raise

    # The write succeeded — this user has write access. Undo it right away.
    try:
        rc.unlink(CANARY)
    except Exception:
        try:
            rc.delete(CANARY)
        except Exception:
            pass

    print("\n" + "=" * 70)
    print("STOP — the Redis user has WRITE access.")
    print("The audit must run with a READ-ONLY user to avoid any risk")
    print("of accidental data modification on a production database.")
    print("(The canary key just written was removed immediately.)")
    print("\nCreate a read-only user with this ACL command:\n")
    print(READONLY_ACL_SCRIPT)
    print("=" * 70 + "\n")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 2. Per-node collection helpers
# ---------------------------------------------------------------------------

def collect_info(r: redis.Redis) -> dict:
    return r.info("all")


def collect_config(r: redis.Redis) -> dict:
    keys = [
        "maxclients", "timeout", "tcp-keepalive",
        "maxmemory", "maxmemory-policy",
        "slowlog-log-slower-than", "slowlog-max-len",
        "bind", "protected-mode",
        "hz", "dynamic-hz", "lazyfree-lazy-eviction",
        "appendonly", "save",
        "tls-port", "aclfile",
        "latency-monitor-threshold",
    ]
    result = {}
    for key in keys:
        try:
            result.update(r.config_get(key))
        except (redis.exceptions.ResponseError, redis.exceptions.NoPermissionError):
            result[key] = "N/A"
    return result


def collect_slowlog(r: redis.Redis) -> list:
    try:
        entries = r.slowlog_get(SLOWLOG_ENTRIES)
    except (redis.exceptions.ResponseError, redis.exceptions.NoPermissionError):
        return []
    return [
        {
            "id":          e.get("id"),
            "duration_us": e.get("duration"),
            "duration_ms": round(e.get("duration", 0) / 1000, 2),
            "command":     " ".join(str(a) for a in e.get("command", [])) if isinstance(e.get("command"), (list, tuple)) else str(e.get("command", "")),
            "timestamp":   datetime.fromtimestamp(e.get("start_time", 0)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for e in entries
    ]


def collect_client_list(r: redis.Redis) -> list:
    try:
        return r.client_list()
    except (redis.exceptions.ResponseError, redis.exceptions.NoPermissionError):
        return []


def collect_memory_doctor(r: redis.Redis) -> str:
    try:
        return r.execute_command("MEMORY DOCTOR")
    except (redis.exceptions.ResponseError, redis.exceptions.NoPermissionError):
        return "N/A"


def collect_latency(r: redis.Redis) -> list:
    """LATENCY LATEST returns the most recent latency spike per event type."""
    try:
        raw = r.execute_command("LATENCY LATEST")
        return [
            {"event": item[0], "latest_ms": item[2], "max_ms": item[3]}
            for item in (raw or [])
        ]
    except (redis.exceptions.ResponseError, redis.exceptions.NoPermissionError):
        return []


HOTKEYS_MIN_VERSION = (8, 6)  # HOTKEYS command introduced in Redis 8.6


def version_at_least(version_str, minimum) -> bool:
    """True if a dotted version string (e.g. '8.6.5') is >= the minimum tuple."""
    try:
        nums = [int(x) for x in str(version_str).split(".")]
    except (ValueError, TypeError):
        return False
    nums += [0] * (len(minimum) - len(nums))
    return tuple(nums[:len(minimum)]) >= tuple(minimum)


def _parse_hotkeys_get(reply):
    """
    Parse a HOTKEYS GET reply into {by_cpu, by_net, total_cpu, total_net,
    duration_ms}, or None. Handles RESP2 (flat [k, v, ...] lists) and RESP3
    (dicts) — verified against Redis 8.6.5.
    """
    if not reply:
        return None
    data = reply[0] if isinstance(reply, list) and reply else reply
    if not isinstance(data, dict):
        return None

    def _to_dict(flat):
        if isinstance(flat, dict):
            return flat
        if isinstance(flat, (list, tuple)):
            return {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}
        return {}

    return {
        "by_cpu":      _to_dict(data.get("by-cpu-time-us", [])),
        "by_net":      _to_dict(data.get("by-net-bytes", [])),
        "total_cpu":   data.get("all-commands-all-slots-us", 0) or 0,
        "total_net":   data.get("total-net-bytes", 0) or 0,
        "duration_ms": data.get("collection-duration-ms", 0) or 0,
    }


def collect_hotkeys(r: redis.Redis, duration_s: int, top_k: int = 10):
    """
    INVASIVE (opt-in): run server-side HOTKEYS tracking for duration_s seconds,
    then fetch and release it. Requires Redis >= 8.6 and a user permitted to run
    HOTKEYS. Returns parsed results, or None if unavailable / not permitted.
    """
    try:
        r.execute_command("HOTKEYS", "START", "METRICS", "2", "CPU", "NET",
                          "COUNT", str(top_k), "DURATION", str(duration_s))
    except (redis.exceptions.ResponseError, redis.exceptions.NoPermissionError) as e:
        print(f"    HOTKEYS unavailable on this node ({e}); grant +HOTKEYS or use Redis >= 8.6.")
        return None

    time.sleep(duration_s + 1)   # let the tracking window elapse (auto-stops at DURATION)
    try:
        reply = r.execute_command("HOTKEYS", "GET")
    except (redis.exceptions.ResponseError, redis.exceptions.NoPermissionError):
        reply = None
    finally:
        for sub in ("STOP", "RESET"):   # release tracking resources, best-effort
            try:
                r.execute_command("HOTKEYS", sub)
            except redis.exceptions.ResponseError:
                pass
    return _parse_hotkeys_get(reply)


def collect_acl_data(r: redis.Redis) -> dict:
    """Fetch ACL users list and recent ACL log entries."""
    acl_list = []
    acl_log  = []

    try:
        raw_list = r.execute_command("ACL LIST")
        acl_list = [_parse_acl_entry(line) for line in (raw_list or [])]
    except (redis.exceptions.ResponseError, redis.exceptions.NoPermissionError):
        pass

    try:
        raw_log = r.execute_command("ACL LOG")
        for entry in (raw_log or []):
            if isinstance(entry, dict):
                acl_log.append(entry)
            elif isinstance(entry, list):
                # Older redis-py may return flat lists: [key, val, key, val, ...]
                acl_log.append(dict(zip(entry[::2], entry[1::2])))
    except (redis.exceptions.ResponseError, redis.exceptions.NoPermissionError):
        pass

    return {"users": acl_list, "log": acl_log}


def _parse_acl_entry(entry: str) -> dict:
    """
    Parse one ACL LIST line:
      user <name> on|off [nopass|#hash] [~pattern] [&pattern] [+cmd|-cmd ...]
    """
    parts = entry.split()
    name    = parts[1] if len(parts) > 1 else "?"
    enabled = parts[2] == "on" if len(parts) > 2 else False

    nopass       = "nopass" in parts
    has_password = any(p.startswith("#") for p in parts)
    key_patterns = [p for p in parts if p.startswith("~")]
    chan_patterns = [p for p in parts if p.startswith("&")]
    commands     = [p for p in parts if p.startswith("+") or p.startswith("-")]

    return {
        "name":             name,
        "enabled":          enabled,
        "nopass":           nopass,
        "has_password":     has_password,
        "key_patterns":     key_patterns,
        "channel_patterns": chan_patterns,
        "commands":         commands,
        "has_all_keys":     "~*" in key_patterns,
        "has_all_commands": "+@all" in commands,
        "has_dangerous":    any(c in commands for c in ("+@dangerous", "+@admin", "+@all")),
        "has_write":        any(c in commands for c in ("+@write", "+@all", "+set", "+del", "+flushdb", "+flushall")),
        "raw":              entry,
    }


# ---------------------------------------------------------------------------
# 3. Key space analysis — SCAN per shard (never KEYS *)
# ---------------------------------------------------------------------------

def scan_keyspace(node_connections: list) -> dict:
    """
    Scan up to SCAN_SAMPLE_SIZE keys per shard.
    Collects: TTL distribution, type distribution, memory per key (for big keys).
    Also tracks keys expiring within the next hour (potential burst expiry risk).
    """
    ttl_dist   = defaultdict(int)
    type_dist  = defaultdict(int)
    big_keys   = []
    expiring_soon = []  # keys with TTL < 3600s

    for r in node_connections:
        scanned = 0
        cursor  = 0

        while True:
            cursor, keys = r.scan(cursor=cursor, count=100)

            remaining = SCAN_SAMPLE_SIZE - scanned
            batch = keys[:remaining] if remaining > 0 else []
            if batch:
                # One pipeline per SCAN page instead of 3 blocking round-trips
                # per key. These are direct per-node connections, so there is
                # no CROSSSLOT concern — the node owns all the keys it returned.
                pipe = r.pipeline(transaction=False)
                for key in batch:
                    pipe.type(key)
                    pipe.ttl(key)
                    pipe.memory_usage(key, samples=SCAN_MEMORY_SAMPLES)
                results = pipe.execute()

                for i, key in enumerate(batch):
                    key_type = results[i * 3]
                    ttl      = results[i * 3 + 1]
                    size     = results[i * 3 + 2] or 0

                    # Key expired between SCAN and read — skip the ghost so it
                    # doesn't pollute the type/TTL distributions or big-keys.
                    if key_type == "none":
                        continue

                    type_dist[key_type] += 1

                    if ttl == -1:
                        ttl_dist["no_ttl"] += 1
                    elif ttl == -2:
                        ttl_dist["expired"] += 1
                    else:
                        ttl_dist["has_ttl"] += 1
                        if ttl < 3600:
                            expiring_soon.append({"key": key, "ttl": ttl})

                    big_keys.append({"key": key, "size_bytes": size, "type": key_type, "ttl": ttl})
                    scanned += 1

            if cursor == 0 or scanned >= SCAN_SAMPLE_SIZE:
                break

    big_keys.sort(key=lambda x: x["size_bytes"], reverse=True)
    expiring_soon.sort(key=lambda x: x["ttl"])

    return {
        "ttl_distribution":  dict(ttl_dist),
        "type_distribution": dict(type_dist),
        "big_keys":          big_keys[:BIG_KEY_TOP_N],
        "expiring_soon":     expiring_soon[:20],
        "total_scanned":     sum(type_dist.values()),
    }


# ---------------------------------------------------------------------------
# 4. Connection analysis
# ---------------------------------------------------------------------------

def analyse_connections(client_list_per_node: dict) -> dict:
    per_ip       = defaultdict(int)
    idle_buckets = {"<10s": 0, "10s-1min": 0, "1min-10min": 0, ">10min": 0}
    cmd_dist     = defaultdict(int)
    total        = 0
    default_user_external = 0   # app authenticating as 'default' from a non-loopback IP

    for clients in client_list_per_node.values():
        for c in clients:
            total += 1
            ip = c.get("addr", "unknown").split(":")[0]
            per_ip[ip] += 1

            idle = int(c.get("idle", 0))
            if idle < 10:
                idle_buckets["<10s"] += 1
            elif idle < 60:
                idle_buckets["10s-1min"] += 1
            elif idle < 600:
                idle_buckets["1min-10min"] += 1
            else:
                idle_buckets[">10min"] += 1

            cmd_dist[c.get("cmd", "unknown")] += 1

            if c.get("user") == "default" and not ip.startswith("127.") and ip != "unknown":
                default_user_external += 1

    return {
        "total":        total,
        "per_ip":       dict(sorted(per_ip.items(), key=lambda x: x[1], reverse=True)),
        "idle_buckets": idle_buckets,
        "cmd_dist":     dict(sorted(cmd_dist.items(), key=lambda x: x[1], reverse=True)),
        "default_user_external": default_user_external,
    }


# ---------------------------------------------------------------------------
# 5. Performance stats summary (from INFO)
# ---------------------------------------------------------------------------

def compute_stats_summary(all_node_data: list) -> dict:
    """Aggregate hit ratio, evictions, bandwidth across all nodes."""
    total_hits      = sum(nd["info"].get("keyspace_hits", 0)   for nd in all_node_data)
    total_misses    = sum(nd["info"].get("keyspace_misses", 0) for nd in all_node_data)
    total_evictions = sum(nd["info"].get("evicted_keys", 0)    for nd in all_node_data)
    total_expired   = sum(nd["info"].get("expired_keys", 0)    for nd in all_node_data)
    total_ops       = sum(nd["info"].get("instantaneous_ops_per_sec", 0) for nd in all_node_data)
    total_net_in    = sum(nd["info"].get("total_net_input_bytes", 0)     for nd in all_node_data)
    total_net_out   = sum(nd["info"].get("total_net_output_bytes", 0)    for nd in all_node_data)
    total_cmds      = sum(nd["info"].get("total_commands_processed", 0)  for nd in all_node_data)

    denominator = total_hits + total_misses
    hit_ratio   = total_hits / denominator if denominator > 0 else None

    return {
        "hit_ratio":      hit_ratio,
        "total_hits":     total_hits,
        "total_misses":   total_misses,
        "total_evictions": total_evictions,
        "total_expired":  total_expired,
        "total_ops_per_sec": total_ops,
        "total_net_in_bytes":  total_net_in,
        "total_net_out_bytes": total_net_out,
        "total_commands":      total_cmds,
    }


# ---------------------------------------------------------------------------
# 6. Security analysis
# ---------------------------------------------------------------------------

def analyse_security(all_node_data: list) -> dict:
    """
    Consolidate security findings from config + ACL data across EVERY node.

    In a Redis Cluster, ACLs and network config are per-node, so each primary
    is inspected. Identical findings are deduplicated; a finding present on only
    some nodes is annotated with the affected node labels.
    """
    if not all_node_data:
        return {"findings": [], "users": [], "acl_log": []}

    n_nodes = len(all_node_data)
    agg = {}  # (category, title) -> {severity, category, title, detail, nodes:set}

    def add(severity, category, title, detail, node):
        entry = agg.get((category, title))
        if entry is None:
            agg[(category, title)] = {
                "severity": severity, "category": category,
                "title": title, "detail": detail, "nodes": {node},
            }
        else:
            entry["nodes"].add(node)

    for nd in all_node_data:
        label    = nd.get("label", "?")
        cfg      = nd.get("config", {})
        acl_data = nd.get("acl_data", {})
        users    = acl_data.get("users", [])
        acl_log  = acl_data.get("log", [])

        # ── ACL user analysis ─────────────────────────────────────────────────
        for user in users:
            name = user["name"]

            if name == "default" and user["enabled"] and user["nopass"]:
                add("CRITICAL", "ACL", "Default user active with no password (nopass)",
                    "The 'default' user has no password. Any client can connect "
                    "without authentication. Disable or password-protect the default user: "
                    "ACL SETUSER default off", label)

            if name == "default" and user["enabled"] and user["has_all_commands"]:
                add("HIGH", "ACL", "Default user has full command access (+@all)",
                    "The default user can run every command including FLUSHALL, CONFIG, DEBUG. "
                    "Restrict to minimum required commands or disable the default user.", label)

            if user["enabled"] and user["has_all_commands"] and name != "default":
                add("HIGH", "ACL", f"User '{name}' has unrestricted command access (+@all)",
                    f"User '{name}' can execute any Redis command. Scope down to only the "
                    "commands this user actually needs.", label)

            if user["enabled"] and user["has_dangerous"] and not user["has_all_commands"] and name != "default":
                add("MEDIUM", "ACL", f"User '{name}' has access to dangerous command category",
                    f"User '{name}' has +@dangerous or +@admin. These categories include "
                    "DEBUG, CONFIG (write), FLUSHALL, SLAVEOF, etc. Review if this is intentional.", label)

            if user["enabled"] and user["has_write"] and user["has_all_keys"] and name != "default":
                add("MEDIUM", "ACL", f"User '{name}' can write to all keys (~*)",
                    f"User '{name}' has write access to all keyspaces (~*). "
                    "Restrict key patterns to the namespace this user owns (e.g. ~app:*).", label)

        # Flag if no ACL data was available (likely no ACL configured at all)
        if not users:
            add("HIGH", "ACL", "ACL data unavailable — may be running without ACL",
                "Could not retrieve ACL LIST. Redis may be running without ACL configuration "
                "(pre-Redis 6 mode or permission denied). All clients share the same access level.", label)

        # ── Network exposure ──────────────────────────────────────────────────
        bind_val = cfg.get("bind", "")
        if "0.0.0.0" in bind_val or bind_val == "":
            add("HIGH", "Network", f"Redis listening on all interfaces (bind: '{bind_val or 'unset'}')",
                "Redis is reachable from any network interface. "
                "Bind to the specific interface used by application servers only "
                "(e.g. bind 127.0.0.1 10.0.0.5).", label)

        if cfg.get("protected-mode") == "no":
            add("HIGH", "Network", "protected-mode is disabled",
                "With protected-mode off, Redis accepts connections from any IP "
                "even without a password when bind is not restricted. "
                "Re-enable: CONFIG SET protected-mode yes", label)

        tls_port = cfg.get("tls-port", "0")
        if str(tls_port) in ("0", "", "N/A"):
            add("MEDIUM", "Network", "TLS not configured",
                "Connections to Redis are unencrypted. Credentials and data travel in clear text. "
                "Configure tls-port and provide certificates (tls-cert-file, tls-key-file).", label)

        # ── Persistence / data safety ─────────────────────────────────────────
        aof  = cfg.get("appendonly", "no")
        save = cfg.get("save", "")
        if aof == "no" and (not save or save == '""' or save == ""):
            add("MEDIUM", "Persistence", "No persistence configured (no RDB save, no AOF)",
                "All data will be lost on restart. For a cache this may be acceptable, "
                "but the application must be able to handle a cold cache gracefully. "
                "Consider enabling at minimum RDB snapshots (save 900 1).", label)

        # ── ACL log — recent auth failures / permission denials ───────────────
        auth_failures = [e for e in acl_log if str(e.get("reason", "")).lower() in ("auth", "noauth")]
        # ACL LOG aggregates repeated identical failures into a single entry with
        # a `count`, so sum the counts rather than counting entries — otherwise a
        # brute-force from one source (one entry, high count) slips past.
        auth_failure_count = sum(int(e.get("count", 1) or 1) for e in auth_failures)
        if auth_failure_count > 5:
            add("MEDIUM", "ACL Log", f"{auth_failure_count} recent authentication failures in ACL LOG",
                "Multiple failed authentication attempts. Could indicate a misconfigured "
                "client, leaked credentials, or an active brute-force attempt. "
                "Check the source IPs in the ACL LOG.", label)

        perm_denials = [e for e in acl_log if str(e.get("reason", "")).lower() == "command"]
        if perm_denials:
            add("LOW", "ACL Log", f"{len(perm_denials)} recent permission-denied entries in ACL LOG",
                "A client is trying to run commands it is not allowed to execute. "
                "This may indicate a misconfigured application account. "
                "Review the ACL LOG for usernames and commands.", label)

    findings = []
    for entry in agg.values():
        detail = entry["detail"]
        if len(entry["nodes"]) < n_nodes:
            detail = f"{detail} (Affected nodes: {', '.join(sorted(entry['nodes']))})"
        findings.append({
            "severity": entry["severity"], "category": entry["category"],
            "title": entry["title"], "detail": detail,
        })

    # Representative ACL user list / log for the report tables (first node).
    first_acl = all_node_data[0].get("acl_data", {})
    return {"findings": findings, "users": first_acl.get("users", []),
            "acl_log": first_acl.get("log", [])}


# ---------------------------------------------------------------------------
# 7. Recommendations (performance + security)
# ---------------------------------------------------------------------------

def generate_recommendations(all_node_data: list, conn_analysis: dict,
                              keyspace: dict, security: dict, stats: dict) -> list:
    recs = []

    # ── Connection freeze ────────────────────────────────────────────────────
    for nd in all_node_data:
        if nd["config"].get("timeout") == "0":
            recs.append({
                "severity": "CRITICAL",
                "title":    "timeout=0 — idle connections never close",
                "detail":   "Idle client connections accumulate indefinitely. When maxclients is reached "
                            "Redis refuses new connections — this matches the reported freeze. "
                            "Fix: CONFIG SET timeout 300 && CONFIG SET tcp-keepalive 60",
            })
            break

    # ── Security findings as recommendations ─────────────────────────────────
    for finding in security["findings"]:
        recs.append({
            "severity": finding["severity"],
            "title":    f"[Security / {finding['category']}] {finding['title']}",
            "detail":   finding["detail"],
        })

    # ── Memory ───────────────────────────────────────────────────────────────
    for nd in all_node_data:
        if nd["config"].get("maxmemory") in ("0", 0):
            recs.append({
                "severity": "HIGH",
                "title":    "maxmemory=0 — no memory cap",
                "detail":   "Redis has no memory limit. A memory leak or sudden data burst will OOM-kill "
                            "the process. Set maxmemory to 80% of available RAM and pick an eviction policy "
                            "(allkeys-lru is typical for session/cache workloads).",
            })
            break

    # ── Replication ──────────────────────────────────────────────────────────
    for nd in all_node_data:
        if nd["info"].get("connected_slaves", 0) == 0 and nd["info"].get("role") == "master":
            recs.append({
                "severity": "HIGH",
                "title":    "No replicas — single point of failure per shard",
                "detail":   "Every shard is a standalone master. A single node failure causes data loss "
                            "and application downtime. Add at least one replica per shard (cluster-replicas 1).",
            })
            break

    # ── Hit ratio ────────────────────────────────────────────────────────────
    if stats["hit_ratio"] is not None and stats["hit_ratio"] < 0.80:
        pct = round(stats["hit_ratio"] * 100, 1)
        recs.append({
            "severity": "MEDIUM",
            "title":    f"Cache hit ratio is low: {pct}%",
            "detail":   f"Only {pct}% of GET-type commands hit the cache. "
                        "This means frequent misses force the application to reload from the database. "
                        "Investigate key TTLs, eviction policy, and whether the cache is being warmed correctly.",
        })

    # ── Evictions ─────────────────────────────────────────────────────────────
    if stats["total_evictions"] > 0:
        recs.append({
            "severity": "MEDIUM",
            "title":    f"{stats['total_evictions']:,} keys evicted",
            "detail":   "Redis is evicting keys due to memory pressure. This causes artificial cache misses. "
                        "Either increase maxmemory, review which keys should persist, or switch to a more "
                        "aggressive eviction policy (allkeys-lru).",
        })

    # ── TTL distribution ─────────────────────────────────────────────────────
    no_ttl        = keyspace["ttl_distribution"].get("no_ttl", 0)
    total_scanned = keyspace["total_scanned"]
    if total_scanned > 0 and no_ttl / total_scanned > 0.5:
        pct = round(no_ttl / total_scanned * 100)
        recs.append({
            "severity": "MEDIUM",
            "title":    f"{pct}% of keys have no TTL",
            "detail":   f"{no_ttl} of {total_scanned} sampled keys have no expiry. "
                        "For a cache layer, persistent keys indicate TTL is not being set on writes. "
                        "This leads to unbounded memory growth.",
        })

    # ── Big keys ─────────────────────────────────────────────────────────────
    if keyspace["big_keys"] and keyspace["big_keys"][0]["size_bytes"] > 50_000:
        biggest  = keyspace["big_keys"][0]
        size_kb  = round(biggest["size_bytes"] / 1024, 1)
        recs.append({
            "severity": "MEDIUM",
            "title":    f"Large keys detected (biggest: {size_kb} KB)",
            "detail":   f"Key '{biggest['key']}' uses {size_kb} KB. Large values increase "
                        "serialisation time, memory fragmentation, and block the Redis event loop. "
                        "Split large hashes/lists or compress values in the application.",
        })

    # ── Idle connections ─────────────────────────────────────────────────────
    idle_long = conn_analysis["idle_buckets"].get(">10min", 0)
    if idle_long > 10:
        recs.append({
            "severity": "MEDIUM",
            "title":    f"{idle_long} connections idle > 10 minutes",
            "detail":   "Stale idle connections waste file descriptors and memory slots. "
                        "Configure the Java connection pool to recycle idle connections "
                        "(testWhileIdle, minEvictableIdleTimeMillis in Jedis/Lettuce/HikariCP). "
                        "Also set CONFIG SET timeout 300 on Redis side.",
        })

    # ── Slowlog threshold ────────────────────────────────────────────────────
    for nd in all_node_data:
        threshold = nd["config"].get("slowlog-log-slower-than", "")
        try:
            if int(threshold) > 100_000:
                recs.append({
                    "severity": "LOW",
                    "title":    f"slowlog-log-slower-than={threshold}µs is too permissive",
                    "detail":   f"Only commands slower than {int(threshold)//1000} ms are logged. "
                                "Lower to 10000µs (10ms) to catch more problematic commands.",
                })
                break
        except (ValueError, TypeError):
            pass

    # ── Eviction policy on a capped cache ─────────────────────────────────────
    for nd in all_node_data:
        maxmem = str(nd["config"].get("maxmemory", "0"))
        if maxmem not in ("0", "", "N/A") and nd["config"].get("maxmemory-policy") == "noeviction":
            recs.append({
                "severity": "MEDIUM",
                "title":    "maxmemory-policy is noeviction on a capped instance",
                "detail":   "With a maxmemory cap and noeviction, Redis rejects writes with an OOM "
                            "error once full instead of evicting cold data. For a cache, use "
                            "allkeys-lru or allkeys-lfu.",
            })
            break

    # ── Application connecting as the 'default' user ──────────────────────────
    if conn_analysis.get("default_user_external", 0) > 0:
        recs.append({
            "severity": "HIGH",
            "title":    "Application connecting as the 'default' user",
            "detail":   f"{conn_analysis['default_user_external']} non-loopback client(s) are "
                        "authenticated as 'default'. Applications should use a dedicated, "
                        "least-privilege ACL user scoped to the keys and commands they need.",
        })

    # ── Connection concentration from a single IP ─────────────────────────────
    for ip, count in conn_analysis.get("per_ip", {}).items():
        if count > CONN_PER_IP_WARN and ip not in ("127.0.0.1", "unknown"):
            recs.append({
                "severity": "MEDIUM",
                "title":    f"{count} connections from a single IP ({ip})",
                "detail":   "A single host holds a very large number of connections — often an "
                            "oversized or leaking client pool, or no connection multiplexing. "
                            "Right-size the pool and enable idle-connection eviction.",
            })
            break

    # ── Monolithic-String data model ──────────────────────────────────────────
    total_scanned = keyspace.get("total_scanned", 0)
    types = keyspace.get("type_distribution", {})
    big_keys = keyspace.get("big_keys", [])
    if total_scanned > 0 and types.get("string", 0) / total_scanned >= 0.9:
        biggest = big_keys[0] if big_keys else None
        if biggest and biggest.get("type") == "string" and biggest.get("size_bytes", 0) >= 100_000:
            recs.append({
                "severity": "MEDIUM",
                "title":    "Data modelled as large monolithic strings",
                "detail":   "Almost all keys are Strings and the largest values are big serialised "
                            "blobs. Reading one field means fetching and deserialising the whole "
                            "value. Model records as Hash or JSON for field-level access "
                            "(HGET / JSON.GET), and cap value size.",
            })

    # ── Memory fragmentation ──────────────────────────────────────────────────
    for nd in all_node_data:
        try:
            frag = float(nd["info"].get("mem_fragmentation_ratio", 0))
        except (ValueError, TypeError):
            continue
        if frag > 1.5:
            recs.append({
                "severity": "MEDIUM",
                "title":    f"High memory fragmentation ratio ({frag})",
                "detail":   "The allocator holds much more RSS than live data. Consider enabling "
                            "activedefrag, running MEMORY PURGE, or (last resort) a rolling restart. "
                            "Often follows a memory peak — check used_memory_peak.",
            })
            break

    # ── Latency monitoring ────────────────────────────────────────────────────
    for nd in all_node_data:
        if str(nd["config"].get("latency-monitor-threshold", "0")) in ("0", "", "N/A"):
            recs.append({
                "severity": "LOW",
                "title":    "Latency monitor disabled (latency-monitor-threshold=0)",
                "detail":   "The built-in latency monitor is off, so LATENCY LATEST stays empty and "
                            "spikes go unrecorded. Set latency-monitor-threshold (e.g. 100 ms).",
            })
            break
    if any(nd.get("latency") for nd in all_node_data):
        recs.append({
            "severity": "MEDIUM",
            "title":    "Latency spikes recorded (LATENCY LATEST)",
            "detail":   "One or more nodes recorded latency events. Inspect LATENCY LATEST / "
                        "LATENCY HISTORY per event type (fork, aof-write, expire-cycle, command) "
                        "to find the cause.",
        })

    # ── Both AOF and RDB enabled ──────────────────────────────────────────────
    for nd in all_node_data:
        aof  = nd["config"].get("appendonly", "no")
        save = nd["config"].get("save", "")
        if aof == "yes" and save and save not in ('""', "", "N/A"):
            recs.append({
                "severity": "LOW",
                "title":    "Both AOF and RDB persistence are enabled",
                "detail":   "For a pure cache whose data is reconstructible, running both AOF "
                            "(fsync) and RDB (fork) adds I/O and latency for little benefit. "
                            "Consider RDB-only, or no persistence, if a cold cache is acceptable.",
            })
            break

    # ── Hot key (heuristic from slow log; prefer HOTKEYS on Redis >= 8.6) ──────
    slow_keys = []
    for nd in all_node_data:
        for e in nd.get("slowlog", []):
            parts = str(e.get("command", "")).split()
            if len(parts) >= 2:
                slow_keys.append(parts[1])
    if slow_keys:
        top_key, top_count = Counter(slow_keys).most_common(1)[0]
        if top_count >= 5 and top_count / len(slow_keys) >= 0.5:
            recs.append({
                "severity": "MEDIUM",
                "title":    f"Hot key in slow log: {top_key} ({top_count} slow ops)",
                "detail":   "A single key dominates the slow log — a hot key concentrating load on "
                            "one shard's single thread. Split the value, add a client-side "
                            "near-cache (CLIENT TRACKING), or shard it. On Redis >= 8.6, re-run "
                            "with --hotkeys N for precise per-key CPU/network metrics.",
            })

    return recs


HOTKEY_SHARE_WARN = 0.5  # a key taking >= this share of a shard's CPU or net is "hot"


def analyse_hotkeys(all_node_data: list) -> list:
    """Recommendations from HOTKEYS tracking data (present only with --hotkeys)."""
    recs = []
    for nd in all_node_data:
        hk = nd.get("hotkeys")
        if not hk:
            continue
        for metric_key, total_key, unit in (
            ("by_cpu", "total_cpu", "CPU time"),
            ("by_net", "total_net", "network bytes"),
        ):
            metric = hk.get(metric_key) or {}
            total = hk.get(total_key) or 0
            if not metric or total <= 0:
                continue
            top_key = max(metric, key=lambda k: metric[k])
            share = metric[top_key] / total
            if share >= HOTKEY_SHARE_WARN:
                recs.append({
                    "severity": "MEDIUM",
                    "title":    f"Hot key {top_key} — {round(share * 100)}% of {unit} on {nd['label']}",
                    "detail":   f"HOTKEYS tracking shows one key dominating this shard's {unit}, "
                                "concentrating load on a single thread. Split the value, add a "
                                "client-side near-cache (CLIENT TRACKING), or shard the key.",
                })
    return recs


# ---------------------------------------------------------------------------
# 8. HTML report
# ---------------------------------------------------------------------------

SEVERITY_COLOR = {
    "CRITICAL": "#d32f2f",
    "HIGH":     "#f57c00",
    "MEDIUM":   "#f9a825",
    "LOW":      "#388e3c",
}

CSS = """
body { font-family: Arial, sans-serif; margin: 0; background: #f5f5f5; color: #212121; }
.page { max-width: 1100px; margin: 0 auto; padding: 32px 24px; }
h1 { color: #b71c1c; margin-bottom: 4px; }
.meta { color: #757575; font-size: 14px; margin-bottom: 32px; }
h2 { color: #c62828; border-bottom: 2px solid #ef9a9a; padding-bottom: 6px; margin-top: 40px; }
h3 { color: #424242; margin-top: 20px; margin-bottom: 8px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; background: #fff; margin-bottom: 16px; }
th { background: #c62828; color: #fff; padding: 8px 12px; text-align: left; }
td { padding: 7px 12px; border-bottom: 1px solid #e0e0e0; vertical-align: top; }
tr:hover td { background: #fce4e4; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; color: #fff; }
.rec { background: #fff; border-left: 5px solid #ccc; padding: 12px 16px; margin: 10px 0; border-radius: 4px; }
.rec .title { font-weight: bold; margin-bottom: 4px; }
.rec .detail { font-size: 14px; color: #424242; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.card { background: #fff; padding: 16px; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,.12); }
.card h3 { margin-top: 0; }
code { background: #f5f5f5; padding: 2px 6px; border-radius: 3px; font-size: 13px; }
pre  { background: #212121; color: #e0e0e0; padding: 16px; border-radius: 6px; overflow-x: auto; font-size: 12px; line-height: 1.5; }
.ok   { color: #2e7d32; font-weight: bold; }
.warn { color: #e65100; font-weight: bold; }
.crit { color: #b71c1c; font-weight: bold; }
.stat-box { background:#fff; border-radius:6px; padding:16px 20px; box-shadow:0 1px 3px rgba(0,0,0,.12); text-align:center; }
.stat-box .val { font-size: 28px; font-weight: bold; color: #c62828; }
.stat-box .lbl { font-size: 12px; color: #757575; margin-top: 4px; }
.stats-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap:12px; margin-bottom:24px; }
"""


def h(text) -> str:
    return html.escape(str(text))


def badge(severity: str) -> str:
    color = SEVERITY_COLOR.get(severity, "#757575")
    return f'<span class="badge" style="background:{color}">{h(severity)}</span>'


def fmt_bytes(b: int) -> str:
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.1f} GB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MB"
    if b >= 1_024:
        return f"{b / 1_024:.1f} KB"
    return f"{b} B"


# ── HTML sections ────────────────────────────────────────────────────────────

def section_recommendations(recs: list) -> str:
    if not recs:
        return "<h2>Recommendations</h2><p class='ok'>No issues detected.</p>"
    items = ""
    for r in recs:
        color = SEVERITY_COLOR.get(r["severity"], "#757575")
        items += f"""
        <div class="rec" style="border-left-color:{color}">
            <div class="title">{badge(r['severity'])} &nbsp; {h(r['title'])}</div>
            <div class="detail">{h(r['detail'])}</div>
        </div>"""
    return f"<h2>Recommendations ({len(recs)})</h2>{items}"


def section_topology(all_node_data: list) -> str:
    rows = ""
    for nd in all_node_data:
        info = nd["info"]
        rows += f"""
        <tr>
            <td><code>{h(nd['label'])}</code></td>
            <td>{h(info.get('redis_version','?'))}</td>
            <td>{h(info.get('role','?'))}</td>
            <td>{h(info.get('connected_slaves',0))} replicas</td>
            <td>{h(info.get('used_memory_human','?'))}</td>
            <td>{h(info.get('connected_clients','?'))}</td>
            <td>{h(info.get('instantaneous_ops_per_sec','?'))} ops/s</td>
        </tr>"""
    return f"""
    <h2>Cluster Topology</h2>
    <table>
        <tr><th>Node</th><th>Version</th><th>Role</th><th>Replication</th><th>Memory Used</th><th>Connections</th><th>Ops/sec</th></tr>
        {rows}
    </table>"""


def section_stats(stats: dict) -> str:
    hit_pct  = f"{stats['hit_ratio']*100:.1f}%" if stats["hit_ratio"] is not None else "N/A"
    hit_cls  = "crit" if stats["hit_ratio"] is not None and stats["hit_ratio"] < 0.80 else "ok"
    evict_cls = "warn" if stats["total_evictions"] > 0 else "ok"
    return f"""
    <h2>Performance Stats</h2>
    <div class="stats-grid">
        <div class="stat-box">
            <div class="val {hit_cls}">{h(hit_pct)}</div>
            <div class="lbl">Cache Hit Ratio</div>
        </div>
        <div class="stat-box">
            <div class="val">{h(stats['total_ops_per_sec'])}</div>
            <div class="lbl">Total Ops/sec</div>
        </div>
        <div class="stat-box">
            <div class="val">{h(f"{stats['total_hits']:,}")}</div>
            <div class="lbl">Total Hits</div>
        </div>
        <div class="stat-box">
            <div class="val">{h(f"{stats['total_misses']:,}")}</div>
            <div class="lbl">Total Misses</div>
        </div>
        <div class="stat-box">
            <div class="val {evict_cls}">{h(f"{stats['total_evictions']:,}")}</div>
            <div class="lbl">Evicted Keys</div>
        </div>
        <div class="stat-box">
            <div class="val">{h(fmt_bytes(stats['total_net_in_bytes']))}</div>
            <div class="lbl">Net Input (total)</div>
        </div>
        <div class="stat-box">
            <div class="val">{h(fmt_bytes(stats['total_net_out_bytes']))}</div>
            <div class="lbl">Net Output (total)</div>
        </div>
        <div class="stat-box">
            <div class="val">{h(f"{stats['total_commands']:,}")}</div>
            <div class="lbl">Commands Processed</div>
        </div>
    </div>"""


def section_config(all_node_data: list) -> str:
    cfg = all_node_data[0]["config"]
    rows = ""
    important = {
        "maxclients":              "Max concurrent connections (default 10 000)",
        "timeout":                 "Idle connection timeout in seconds — 0 = never ⚠ connections accumulate indefinitely",
        "tcp-keepalive":           "TCP keepalive interval (0 = disabled)",
        "maxmemory":               "Memory cap (0 = unlimited ⚠)",
        "maxmemory-policy":        "Eviction policy when memory cap is reached",
        "slowlog-log-slower-than": "Slowlog threshold µs (10 000 recommended)",
        "slowlog-max-len":         "Max slowlog entries retained",
        "appendonly":              "AOF persistence (yes/no)",
        "save":                    "RDB snapshot schedule",
        "tls-port":                "TLS port (0 or empty = TLS disabled)",
        "aclfile":                 "External ACL file path (empty = inline ACL)",
        "hz":                      "Background tasks frequency",
        "lazyfree-lazy-eviction":  "Async key eviction",
    }
    for key, desc in important.items():
        val = cfg.get(key, "N/A")
        rows += f"<tr><td><code>{h(key)}</code></td><td>{h(val)}</td><td>{h(desc)}</td></tr>"
    return f"""
    <h2>Configuration</h2>
    <p>From <code>CONFIG GET</code> on first node (typically identical across all shards).</p>
    <table>
        <tr><th>Parameter</th><th>Value</th><th>Description</th></tr>
        {rows}
    </table>"""


def section_memory(all_node_data: list) -> str:
    rows = ""
    for nd in all_node_data:
        info = nd["info"]
        frag = info.get("mem_fragmentation_ratio", 0)
        frag_cls = "crit" if frag > 1.5 else ("warn" if frag > 1.2 else "ok")
        rows += f"""
        <tr>
            <td><code>{h(nd['label'])}</code></td>
            <td>{h(info.get('used_memory_human','?'))}</td>
            <td>{h(info.get('used_memory_peak_human','?'))}</td>
            <td>{h(info.get('maxmemory_human','unlimited'))}</td>
            <td><span class="{frag_cls}">{h(frag)}</span></td>
            <td style="font-size:13px">{h(nd.get('memory_doctor',''))}</td>
        </tr>"""
    return f"""
    <h2>Memory</h2>
    <table>
        <tr><th>Node</th><th>Used</th><th>Peak</th><th>Limit</th><th>Fragmentation ratio</th><th>MEMORY DOCTOR</th></tr>
        {rows}
    </table>"""


def section_latency(all_node_data: list) -> str:
    all_entries = []
    for nd in all_node_data:
        for e in nd.get("latency", []):
            all_entries.append({**e, "node": nd["label"]})
    if not all_entries:
        return "<h2>Latency</h2><p>No latency events recorded (LATENCY LATEST is empty — good sign).</p>"
    rows = "".join(
        f"<tr><td><code>{h(e['node'])}</code></td><td>{h(e['event'])}</td>"
        f"<td>{h(e['latest_ms'])} ms</td><td>{h(e['max_ms'])} ms</td></tr>"
        for e in all_entries
    )
    return f"""
    <h2>Latency</h2>
    <table>
        <tr><th>Node</th><th>Event</th><th>Latest spike</th><th>Max recorded</th></tr>
        {rows}
    </table>"""


def section_connections(conn_analysis: dict) -> str:
    ip_rows = "".join(
        f"<tr><td>{h(ip)}</td><td>{h(count)}</td></tr>"
        for ip, count in list(conn_analysis["per_ip"].items())[:20]
    )
    idle_rows = "".join(
        f"<tr><td>{h(bucket)}</td><td>{h(count)}</td></tr>"
        for bucket, count in conn_analysis["idle_buckets"].items()
    )
    cmd_rows = "".join(
        f"<tr><td><code>{h(cmd)}</code></td><td>{h(count)}</td></tr>"
        for cmd, count in list(conn_analysis["cmd_dist"].items())[:15]
    )
    return f"""
    <h2>Connection Analysis <small>({conn_analysis['total']} total across all nodes)</small></h2>
    <div class="grid">
        <div class="card">
            <h3>Connections per source IP (top 20)</h3>
            <table><tr><th>IP</th><th>Connections</th></tr>{ip_rows}</table>
        </div>
        <div class="card">
            <h3>Idle time distribution</h3>
            <table><tr><th>Idle bucket</th><th>Count</th></tr>{idle_rows}</table>
        </div>
    </div>
    <div class="card" style="margin-top:16px">
        <h3>Last command per connection</h3>
        <table><tr><th>Command</th><th>Count</th></tr>{cmd_rows}</table>
    </div>"""


def section_slowlog(all_node_data: list) -> str:
    all_entries = []
    for nd in all_node_data:
        for e in nd["slowlog"]:
            all_entries.append({**e, "node": nd["label"]})
    all_entries.sort(key=lambda x: x["duration_us"], reverse=True)

    if not all_entries:
        return "<h2>Slow Log</h2><p>No slow log entries found.</p>"

    rows = ""
    for e in all_entries[:50]:
        cmd = e["command"]
        display_cmd = cmd[:120] + "…" if len(cmd) > 120 else cmd
        rows += f"""
        <tr>
            <td>{h(e['timestamp'])}</td>
            <td><code>{h(e['node'])}</code></td>
            <td><strong>{h(e['duration_ms'])} ms</strong></td>
            <td><code>{h(display_cmd)}</code></td>
        </tr>"""
    return f"""
    <h2>Slow Log <small>({len(all_entries)} entries — top 50 by duration)</small></h2>
    <table>
        <tr><th>Timestamp</th><th>Node</th><th>Duration</th><th>Command</th></tr>
        {rows}
    </table>"""


def section_keyspace(keyspace: dict, all_node_data: list) -> str:
    def _db_keys(info):
        db0 = info.get("db0")
        if isinstance(db0, dict):       # redis-py parses "db0:keys=N,..." into a dict
            return db0.get("keys", "—")
        return db0 if db0 is not None else "—"

    dbsize_rows = "".join(
        f"<tr><td><code>{h(nd['label'])}</code></td>"
        f"<td>{h(_db_keys(nd['info']))}</td>"
        f"<td>{h(nd['info'].get('expired_keys','?'))} expired</td></tr>"
        for nd in all_node_data
    )
    total_scanned = keyspace["total_scanned"]
    ttl   = keyspace["ttl_distribution"]
    types = keyspace["type_distribution"]
    den         = max(total_scanned, 1)
    no_ttl      = ttl.get("no_ttl", 0)
    has_ttl     = ttl.get("has_ttl", 0)
    expired     = ttl.get("expired", 0)
    no_ttl_pct  = round(no_ttl / den * 100)
    has_ttl_pct = round(has_ttl / den * 100)
    expired_pct = round(expired / den * 100)

    ttl_rows = f"""
        <tr><td>No TTL (persistent)</td><td>{h(no_ttl)}</td>
            <td class="{'crit' if no_ttl_pct>70 else 'warn' if no_ttl_pct>40 else 'ok'}">{no_ttl_pct}%</td></tr>
        <tr><td>Has TTL</td><td>{h(has_ttl)}</td><td>{has_ttl_pct}%</td></tr>
        <tr><td>Expired (vanished during scan)</td><td>{h(expired)}</td><td>{expired_pct}%</td></tr>
    """
    type_rows = "".join(
        f"<tr><td><code>{h(t)}</code></td><td>{h(c)}</td></tr>"
        for t, c in sorted(types.items(), key=lambda x: x[1], reverse=True)
    )

    expiring_rows = ""
    if keyspace["expiring_soon"]:
        expiring_rows = "<h3>Keys expiring in the next hour (sample)</h3><table><tr><th>Key</th><th>TTL (s)</th></tr>"
        for k in keyspace["expiring_soon"][:10]:
            expiring_rows += f"<tr><td><code>{h(k['key'])}</code></td><td>{h(k['ttl'])}</td></tr>"
        expiring_rows += "</table>"

    return f"""
    <h2>Key Space <small>(sample of {total_scanned} keys via SCAN)</small></h2>
    <div class="grid">
        <div class="card">
            <h3>DBSIZE per shard</h3>
            <table><tr><th>Node</th><th>Keyspace</th><th>Expired</th></tr>{dbsize_rows}</table>
        </div>
        <div class="card">
            <h3>TTL distribution</h3>
            <table><tr><th>Category</th><th>Count</th><th>%</th></tr>{ttl_rows}</table>
        </div>
    </div>
    <div class="card" style="margin-top:16px">
        <h3>Data type distribution</h3>
        <table><tr><th>Type</th><th>Count in sample</th></tr>{type_rows}</table>
    </div>
    {expiring_rows}"""


def section_big_keys(keyspace: dict) -> str:
    if not keyspace["big_keys"]:
        return "<h2>Big Keys</h2><p>No keys found in scan.</p>"
    rows = ""
    for i, k in enumerate(keyspace["big_keys"], 1):
        ttl_str = "no TTL" if k["ttl"] == -1 else f"{k['ttl']}s"
        rows += f"""
        <tr>
            <td>{i}</td>
            <td style="word-break:break-all"><code>{h(k['key'])}</code></td>
            <td>{h(k['type'])}</td>
            <td><strong>{h(fmt_bytes(k['size_bytes']))}</strong></td>
            <td>{h(ttl_str)}</td>
        </tr>"""
    return f"""
    <h2>Big Keys <small>(top {len(keyspace['big_keys'])} by memory — SCAN sample)</small></h2>
    <table>
        <tr><th>#</th><th>Key</th><th>Type</th><th>Memory</th><th>TTL</th></tr>
        {rows}
    </table>"""


def section_security(security: dict) -> str:
    users = security["users"]
    findings = security["findings"]
    acl_log  = security["acl_log"]

    # ACL user table
    if users:
        user_rows = ""
        for u in users:
            name_cell = f"<strong>{h(u['name'])}</strong>"
            status    = '<span class="ok">on</span>' if u["enabled"] else '<span style="color:#9e9e9e">off</span>'
            auth_cell = '<span class="crit">nopass</span>' if u["nopass"] else ("password" if u["has_password"] else "—")
            keys_cell = h(", ".join(u["key_patterns"]) or "none")
            cmd_flags = []
            if u["has_all_commands"]:
                cmd_flags.append('<span class="crit">+@all</span>')
            if u["has_dangerous"] and not u["has_all_commands"]:
                cmd_flags.append('<span class="warn">+@dangerous</span>')
            cmds_summary = " ".join(cmd_flags) or f'<span style="color:#9e9e9e">{len(u["commands"])} rules</span>'
            user_rows += f"<tr><td>{name_cell}</td><td>{status}</td><td>{auth_cell}</td><td>{keys_cell}</td><td>{cmds_summary}</td></tr>"
        acl_table = f"""
        <h3>ACL Users</h3>
        <table>
            <tr><th>Username</th><th>Status</th><th>Auth</th><th>Key patterns</th><th>Commands</th></tr>
            {user_rows}
        </table>"""
    else:
        acl_table = "<p>ACL LIST not available (permission denied or ACL not configured).</p>"

    # Security findings
    if findings:
        finding_rows = "".join(
            f"<tr><td>{badge(f['severity'])}</td><td>{h(f['category'])}</td>"
            f"<td><strong>{h(f['title'])}</strong><br><span style='font-size:13px;color:#616161'>{h(f['detail'])}</span></td></tr>"
            for f in findings
        )
        findings_table = f"""
        <h3>Security Findings</h3>
        <table>
            <tr><th>Severity</th><th>Category</th><th>Detail</th></tr>
            {finding_rows}
        </table>"""
    else:
        findings_table = "<p class='ok'>No security findings.</p>"

    # ACL log
    if acl_log:
        log_rows = ""
        for entry in acl_log[:10]:
            log_rows += (
                f"<tr><td>{h(entry.get('reason','?'))}</td>"
                f"<td><code>{h(entry.get('object','?'))}</code></td>"
                f"<td>{h(entry.get('username','?'))}</td>"
                f"<td>{h(str(entry.get('client-info','?'))[:80])}</td>"
                f"<td>{h(entry.get('count','?'))}</td></tr>"
            )
        acl_log_section = f"""
        <h3>ACL Log <small>(last 10 entries)</small></h3>
        <table>
            <tr><th>Reason</th><th>Command / Object</th><th>Username</th><th>Client</th><th>Count</th></tr>
            {log_rows}
        </table>"""
    else:
        acl_log_section = "<h3>ACL Log</h3><p>Empty — no recent auth failures or permission denials.</p>"

    return f"<h2>Security Audit</h2>{findings_table}{acl_table}{acl_log_section}"


def section_hotkeys(all_node_data: list) -> str:
    nodes_with = [nd for nd in all_node_data if nd.get("hotkeys")]
    if not nodes_with:
        return ""   # only shown when the invasive --hotkeys mode was used

    def _rows(metric, total):
        rows = ""
        for k, v in sorted(metric.items(), key=lambda x: x[1], reverse=True):
            pct = f"{v / total * 100:.1f}%" if total else "—"
            rows += f"<tr><td style='word-break:break-all'><code>{h(k)}</code></td><td>{pct}</td></tr>"
        return rows or "<tr><td colspan='2'>no data</td></tr>"

    blocks = ""
    for nd in nodes_with:
        hk = nd["hotkeys"]
        blocks += f"""
        <h3><code>{h(nd['label'])}</code></h3>
        <div class="grid">
            <div class="card"><h3>Top keys by CPU time</h3>
                <table><tr><th>Key</th><th>Share</th></tr>{_rows(hk.get('by_cpu') or {}, hk.get('total_cpu') or 0)}</table></div>
            <div class="card"><h3>Top keys by network bytes</h3>
                <table><tr><th>Key</th><th>Share</th></tr>{_rows(hk.get('by_net') or {}, hk.get('total_net') or 0)}</table></div>
        </div>"""
    return f"""
    <h2>Hot Keys <small>(HOTKEYS tracking — invasive, opt-in)</small></h2>
    <p>Server-side per-key CPU / network share during the tracking window (Redis &ge; 8.6).</p>
    {blocks}"""


def build_html_report(all_node_data, conn_analysis, keyspace, stats, security, recs, duration_s) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nodes_str    = ", ".join(nd["label"] for nd in all_node_data)
    version      = all_node_data[0]["info"].get("redis_version", "?")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Redis Cluster Audit — {generated_at}</title>
  <style>{CSS}</style>
</head>
<body>
<div class="page">
  <h1>Redis Cluster Audit Report</h1>
  <p class="meta">
    Generated: {generated_at} &nbsp;|&nbsp;
    Nodes: {h(nodes_str)} &nbsp;|&nbsp;
    Redis {h(version)} &nbsp;|&nbsp;
    Audit duration: {duration_s:.1f}s
  </p>
  {section_recommendations(recs)}
  {section_topology(all_node_data)}
  {section_stats(stats)}
  {section_config(all_node_data)}
  {section_memory(all_node_data)}
  {section_latency(all_node_data)}
  {section_connections(conn_analysis)}
  {section_slowlog(all_node_data)}
  {section_keyspace(keyspace, all_node_data)}
  {section_big_keys(keyspace)}
  {section_hotkeys(all_node_data)}
  {section_security(security)}
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Read-only audit of a Redis OSS cluster.")
    parser.add_argument(
        "--print-acl", nargs=2, metavar=("USER", "PASSWORD"),
        help="Print the ACL SETUSER line for a read-only audit user, then exit.",
    )
    parser.add_argument(
        "--hotkeys", type=int, metavar="SECONDS",
        help="INVASIVE (opt-in): run HOTKEYS tracking for SECONDS per node to find hot keys "
             "(Redis >= 8.6; needs a user permitted to run HOTKEYS). Mutates server tracking state.",
    )
    args = parser.parse_args()
    if args.print_acl:
        print(build_acl_setuser(args.print_acl[0], args.print_acl[1]))
        return

    t_start = time.time()
    print("Redis Cluster Audit")
    print("=" * 50)

    print("Connecting to cluster...")
    try:
        rc = connect_cluster()
    except Exception as e:
        print(f"ERROR: Cannot connect — {e}")
        sys.exit(1)
    print("  Connected.\n")

    print("Checking permissions (read-only required)...")
    check_permissions(rc)
    print("  OK — user is read-only.\n")

    primaries  = rc.get_primaries()
    if not primaries:
        print("ERROR: No primary nodes found in the cluster — nothing to audit.")
        print("Check cluster health (CLUSTER INFO / CLUSTER NODES) and connectivity.")
        sys.exit(1)
    node_conns = [node_direct_connection(n) for n in primaries]
    print(f"Primaries found: {len(primaries)}")
    for n in primaries:
        print(f"  {n.host}:{n.port}")
    print()

    all_node_data = []
    for i, (node, r) in enumerate(zip(primaries, node_conns), 1):
        label = f"{node.host}:{node.port}"
        print(f"[{i}/{len(primaries)}] Collecting data from {label}...")
        all_node_data.append({
            "label":         label,
            "info":          collect_info(r),
            "config":        collect_config(r),
            "slowlog":       collect_slowlog(r),
            "client_list":   collect_client_list(r),
            "memory_doctor": collect_memory_doctor(r),
            "latency":       collect_latency(r),
            "acl_data":      collect_acl_data(r),
        })

    if args.hotkeys:
        print(f"\n[INVASIVE] HOTKEYS tracking for {args.hotkeys}s per node (Redis >= 8.6)...")
        for nd, r in zip(all_node_data, node_conns):
            version = nd["info"].get("redis_version", "0")
            if not version_at_least(version, HOTKEYS_MIN_VERSION):
                print(f"    {nd['label']}: Redis {version} < 8.6 — skipped")
                continue
            print(f"    {nd['label']}: tracking {args.hotkeys}s...")
            nd["hotkeys"] = collect_hotkeys(r, args.hotkeys)

    print("\nAnalysing connections...")
    client_lists  = {nd["label"]: nd["client_list"] for nd in all_node_data}
    conn_analysis = analyse_connections(client_lists)
    print(f"  {conn_analysis['total']} total connections.")

    print(f"\nScanning key space (up to {SCAN_SAMPLE_SIZE} keys/shard)...")
    keyspace = scan_keyspace(node_conns)
    print(f"  {keyspace['total_scanned']} keys sampled, {len(keyspace['big_keys'])} big keys found.")

    print("\nComputing stats and security analysis...")
    stats    = compute_stats_summary(all_node_data)
    security = analyse_security(all_node_data)
    print(f"  Hit ratio: {stats['hit_ratio']*100:.1f}%" if stats["hit_ratio"] is not None else "  Hit ratio: N/A (no commands yet)")
    print(f"  Security findings: {len(security['findings'])}")

    recs     = generate_recommendations(all_node_data, conn_analysis, keyspace, security, stats)
    recs    += analyse_hotkeys(all_node_data)
    duration = time.time() - t_start

    print(f"\nGenerating HTML report...")
    report   = build_html_report(all_node_data, conn_analysis, keyspace, stats, security, recs, duration)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"  Report written to: {OUTPUT_FILE}")
    print(f"\nAudit complete in {duration:.1f}s")
    print(f"\n{'=' * 50}")
    print(f"RECOMMENDATIONS ({len(recs)} total):")
    for r in recs:
        print(f"  [{r['severity']}] {r['title']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
