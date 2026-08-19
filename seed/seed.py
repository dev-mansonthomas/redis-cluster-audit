"""
Seed the local Docker cluster with realistic test data for audit validation.

What it creates:
  - Big string keys  (10 KB, 100 KB, 1 MB)
  - Big hash keys    (500 and 2 000 fields)
  - Big list keys    (1 000 and 5 000 items)
  - Big set key      (2 000 members)
  - Mix of keys WITH and WITHOUT a TTL  (to test TTL analysis)
  - Normal-size cache keys that look like session/entity data
  - Triggers slow log entries by temporarily lowering the threshold and
    running expensive commands (HGETALL on a big hash, SMEMBERS on a big set)
"""

import os
import sys
import redis
from redis.cluster import RedisCluster, ClusterNode
from dotenv import load_dotenv

load_dotenv()

STARTUP_NODES = [
    ClusterNode("127.0.0.1", 6777),
    ClusterNode("127.0.0.1", 6778),
    ClusterNode("127.0.0.1", 6779),
]

# The Docker containers have fixed internal IPs (172.29.0.x) that aren't
# reachable from the Mac host. address_remap translates MOVED redirect
# targets back to 127.0.0.1:port so all traffic goes through exposed ports.
DOCKER_ADDR_REMAP = {
    ("172.29.0.10", 6777): ("127.0.0.1", 6777),
    ("172.29.0.11", 6778): ("127.0.0.1", 6778),
    ("172.29.0.12", 6779): ("127.0.0.1", 6779),
}

PASSWORD = os.getenv("REDIS_PASSWORD") or None
USERNAME = os.getenv("REDIS_USERNAME") or None


def connect():
    kwargs = dict(
        startup_nodes=STARTUP_NODES,
        password=PASSWORD,
        decode_responses=True,
        require_full_coverage=False,
        address_remap=lambda addr: DOCKER_ADDR_REMAP.get(addr, addr),
    )
    if USERNAME:
        kwargs["username"] = USERNAME
    rc = RedisCluster(**kwargs)
    rc.ping()
    return rc


def get_direct_node_connections(rc):
    """Direct connections to each primary for CONFIG SET / admin commands."""
    nodes = []
    for node in rc.get_primaries():
        kwargs = dict(
            host=node.host,
            port=node.port,
            password=PASSWORD,
            decode_responses=True,
            socket_timeout=5,
        )
        if USERNAME:
            kwargs["username"] = USERNAME
        nodes.append(redis.Redis(**kwargs))
    return nodes


def seed_big_keys(rc):
    print("  Writing big keys...")

    # Big strings — simulate large serialised objects cached by the application
    rc.set("app:cache:bigobject:1mb",  "x" * 1_000_000)
    rc.set("app:cache:bigobject:100kb", "x" * 100_000)
    rc.set("app:cache:bigobject:10kb",  "x" * 10_000)

    # Big hash — simulate a customer record with many attributes
    big_hash_fields = {f"field:{i}": f"value_{i}_{'a' * 50}" for i in range(2000)}
    rc.hset("app:customer:BIG-HASH-2000", mapping=big_hash_fields)

    medium_hash_fields = {f"field:{i}": f"value_{i}" for i in range(500)}
    rc.hset("app:customer:MEDIUM-HASH-500", mapping=medium_hash_fields)

    # Big list — simulate an audit trail or event log
    items = [f"event:{i}:data={'z' * 40}" for i in range(5000)]
    rc.rpush("app:auditlog:BIG-LIST-5000", *items)

    items_medium = [f"event:{i}" for i in range(1000)]
    rc.rpush("app:auditlog:MEDIUM-LIST-1000", *items_medium)

    # Big set — simulate a permission group with many members
    members = [f"user:{i}" for i in range(2000)]
    rc.sadd("app:permissions:BIG-SET-2000", *members)

    print("  Big keys written.")


def seed_normal_keys(rc):
    print("  Writing normal cache keys (with and without TTL)...")

    # Keys without TTL — simulate forgotten/leaking cache entries (no expiry)
    for i in range(200):
        rc.set(f"app:session:no-ttl:{i}", f"sessiondata_{i}")

    for i in range(50):
        rc.hset(f"app:entity:account:{i}", mapping={
            "id": str(i), "balance": str(i * 100), "currency": "TND", "status": "ACTIVE"
        })

    # Keys WITH TTL — correctly configured cache entries
    for i in range(100):
        rc.set(f"app:session:with-ttl:{i}", f"sessiondata_{i}", ex=3600)

    for i in range(50):
        rc.set(f"app:token:{i}", f"jwt_token_{i}", ex=900)

    print("  Normal keys written.")


def generate_hits_and_misses(rc):
    """Drive the cache hit ratio below 80% (mostly misses) so the audit flags it."""
    print("  Generating cache hits/misses...")
    for i in range(50):
        rc.get(f"app:session:with-ttl:{i}")      # hits
    for i in range(500):
        rc.get(f"app:nonexistent:{i}")            # misses
    print("  Hits/misses generated.")


def trigger_slow_logs(node_connections):
    """
    Lower the slowlog threshold to 0 µs so every command is logged,
    then run a few expensive operations to populate the slow log.
    Restores the original threshold afterward.
    """
    print("  Triggering slow log entries...")

    for r in node_connections:
        original = r.config_get("slowlog-log-slower-than")["slowlog-log-slower-than"]
        r.config_set("slowlog-log-slower-than", 0)
        try:
            # These are the exact commands that cause slowness in production:
            # HGETALL on a large hash fetches all fields in one blocking call.
            try:
                r.execute_command("HGETALL", "app:customer:BIG-HASH-2000")
            except Exception:
                pass

            # SMEMBERS on a large set returns all members at once (O(N)).
            try:
                r.execute_command("SMEMBERS", "app:permissions:BIG-SET-2000")
            except Exception:
                pass

            # LRANGE fetching an entire large list
            try:
                r.execute_command("LRANGE", "app:auditlog:BIG-LIST-5000", 0, -1)
            except Exception:
                pass
        finally:
            # Always restore the threshold, even if a command above raised,
            # so we never leave the node logging every command permanently.
            r.config_set("slowlog-log-slower-than", original)

    print("  Slow log entries created.")


def main():
    print("Connecting to local Redis cluster...")
    try:
        rc = connect()
    except Exception as e:
        print(f"ERROR: Cannot connect to cluster — {e}")
        print("Make sure the Docker cluster is running: cd docker && docker-compose up -d")
        print("Then initialise it: bash docker/init-cluster.sh")
        sys.exit(1)

    print("Connected. Flushing existing data...")
    # Flush each shard directly so FLUSHALL doesn't trigger cross-slot issues
    node_conns = get_direct_node_connections(rc)
    for r in node_conns:
        r.flushall()

    seed_big_keys(rc)
    seed_normal_keys(rc)
    generate_hits_and_misses(rc)
    trigger_slow_logs(node_conns)

    # Show a quick summary
    total_keys = sum(r.dbsize() for r in node_conns)
    print(f"\nSeed complete. Total keys in cluster: {total_keys}")
    for r in node_conns:
        info = r.info("server")
        print(f"  {info['redis_version']} @ {r.connection_pool.connection_kwargs['host']}:{r.connection_pool.connection_kwargs['port']} — {r.dbsize()} keys")


if __name__ == "__main__":
    main()
