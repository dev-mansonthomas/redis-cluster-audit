"""
Generate concentrated load on a single big key so HOTKEYS has a clear winner.

Run this in the background while `audit.py --hotkeys N` tracks, so the tracking
window captures a genuine hot key. Reads only (GET) — works as any user that
can read the key. The key is expected to already exist (seeded by seed.py); it
is created here as a fallback if the connection is allowed to write.

Usage: python seed/hotkey_load.py [duration_seconds] [key] [threads]

(memtier_benchmark would also work, e.g.
    memtier_benchmark -s 127.0.0.1 -p 6777 --cluster-mode \
      --command="GET app:cache:bigobject:1mb" --key-pattern=P:P -t 4 -c 4 --test-time=20
 but this Python generator needs no extra install — redis-py is already a dep.)
"""

import os
import sys
import time
import threading

import redis
from redis.cluster import RedisCluster, ClusterNode
from dotenv import load_dotenv

load_dotenv()

STARTUP_NODES = [
    ClusterNode("127.0.0.1", 6777),
    ClusterNode("127.0.0.1", 6778),
    ClusterNode("127.0.0.1", 6779),
]
DOCKER_ADDR_REMAP = {
    ("172.29.0.10", 6777): ("127.0.0.1", 6777),
    ("172.29.0.11", 6778): ("127.0.0.1", 6778),
    ("172.29.0.12", 6779): ("127.0.0.1", 6779),
}
PASSWORD = os.getenv("REDIS_PASSWORD") or None
USERNAME = os.getenv("REDIS_USERNAME") or None


def _client() -> RedisCluster:
    kwargs = dict(
        startup_nodes=STARTUP_NODES,
        password=PASSWORD,
        decode_responses=True,
        require_full_coverage=False,
        address_remap=lambda addr: DOCKER_ADDR_REMAP.get(addr, addr),
    )
    if USERNAME:
        kwargs["username"] = USERNAME
    return RedisCluster(**kwargs)


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    key      = sys.argv[2] if len(sys.argv) > 2 else "app:cache:bigobject:1mb"
    threads  = int(sys.argv[3]) if len(sys.argv) > 3 else 8

    setup = _client()
    try:
        if not setup.exists(key):
            setup.set(key, "x" * 1_000_000)   # fallback; usually created by seed.py
    except redis.exceptions.RedisError:
        pass  # read-only user + key already seeded

    deadline = time.time() + duration

    def worker():
        c = _client()
        while time.time() < deadline:
            try:
                c.get(key)
            except redis.exceptions.RedisError:
                time.sleep(0.05)

    print(f"Hammering '{key}' for {duration:.0f}s with {threads} threads...")
    workers = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    print("Load done.")


if __name__ == "__main__":
    main()
