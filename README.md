# redis-cluster-audit

A read-only audit script for Redis OSS clusters. Connects to a Redis Cluster, collects performance and security data across all shards, and produces a self-contained HTML report.

Read-only by design: it requires a read-only user, and the only write it attempts is a self-expiring canary in its permission check — which a read-only user rejects. If that write unexpectedly succeeds, the key is removed immediately (UNLINK) and the audit aborts before doing anything else.

---

## What it audits

| Section | Details |
|---|---|
| **Recommendations** | Prioritised findings (CRITICAL → LOW) |
| **Cluster Topology** | Nodes, versions, roles, replicas, memory, ops/sec |
| **Performance Stats** | Hit ratio, evictions, bandwidth, commands processed |
| **Configuration** | timeout, maxclients, maxmemory, slowlog, TLS, AOF/RDB |
| **Memory** | Used/peak, limit, fragmentation ratio, MEMORY DOCTOR |
| **Latency** | Spikes per event type (LATENCY LATEST) |
| **Connection Analysis** | Connections per source IP, idle distribution, last command |
| **Slow Log** | Top slow commands across all shards |
| **Key Space** | Key count, TTL distribution, type distribution, keys expiring soon |
| **Big Keys** | Top N keys by memory (SCAN + MEMORY USAGE — never KEYS *) |
| **Security Audit** | ACL users (over-privileged, nopass), network exposure, TLS, ACL LOG |

---

## Prerequisites

| Tool | Min version |
|---|---|
| Python | 3.10+ |
| Docker Desktop | 4.x (local testing only) |
| redis-cli | 6.x (local cluster init only) |

---

## Installation

```bash
pip install -r requirements.txt
```

---

## Quick start — local Docker

Starts a 3-shard Redis 6.2.20 cluster, seeds test data, and runs the audit in one command:

```bash
bash run.sh
```

`run.sh` is self-contained: it installs dependencies, starts the Docker cluster, provisions a read-only user, seeds test data, and runs the audit. It uses fixed test credentials and does **not** read `.env` (that file is only for auditing real, external servers).

---

## Production usage

### Step 1 — Create a read-only audit user on each Redis node

Connect with an admin account and run:

```
ACL SETUSER audit_ro on >YOUR_PASSWORD ~* &* nocommands \
  +INFO +CONFIG|GET \
  +SLOWLOG|GET +SLOWLOG|LEN \
  +MEMORY|USAGE +MEMORY|DOCTOR +MEMORY|STATS \
  +CLIENT|LIST \
  +DBSIZE \
  +CLUSTER|INFO +CLUSTER|NODES +CLUSTER|SLOTS +CLUSTER|SHARDS +CLUSTER|KEYSLOT +CLUSTER|MYID \
  +SCAN +TYPE +TTL +OBJECT|ENCODING +OBJECT|IDLETIME \
  +ACL|LIST +ACL|USERS +ACL|CAT +ACL|LOG \
  +LATENCY|LATEST +LATENCY|HISTORY \
  +PING +READONLY +COMMAND
```

> Generate this exact line for any username/password with:
> ```bash
> python audit.py --print-acl audit_ro YOUR_PASSWORD
> ```
> The script performs a write permission check at startup and exits with the ACL command above if the user has write access.

### Step 2 — Configure `.env`

```bash
cp .env.example .env
```

```env
REDIS_HOST_1=<node-1-ip>
REDIS_PORT_1=6379
REDIS_HOST_2=<node-2-ip>
REDIS_PORT_2=6379
REDIS_HOST_3=<node-3-ip>
REDIS_PORT_3=6379

REDIS_USERNAME=audit_ro
REDIS_PASSWORD=YOUR_PASSWORD

# Keep false for production
REDIS_DOCKER_REMAP=false
```

### Step 3 — Run

```bash
python audit.py
# → report.html
```

---

## Project structure

```
redis-cluster-audit/
├── audit.py              ← main audit script → report.html
├── run.sh                ← full local test (Docker + seed + audit)
├── requirements.txt
├── .env.example          ← production config template
├── docker/
│   ├── docker-compose.yml   ← 3-node Redis 6.2.20 cluster
│   └── init-cluster.sh
└── seed/
    └── seed.py              ← loads test data (big keys, slow logs, TTL mix)
```

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `REDIS_HOST_1/2/3` | 127.0.0.1 | Cluster node addresses |
| `REDIS_PORT_1/2/3` | 6777/6778/6779 | Cluster node ports |
| `REDIS_USERNAME` | — | ACL username (leave empty for default user) |
| `REDIS_PASSWORD` | — | Password (leave empty if none) |
| `BIG_KEY_TOP_N` | 30 | Number of big keys in the report |
| `SCAN_SAMPLE_SIZE` | 5000 | Keys scanned per shard for TTL/type analysis |
| `SLOWLOG_MAX_ENTRIES` | 25 | Slow log entries fetched per node |
| `REDIS_DOCKER_REMAP` | false | Enable only for local Docker testing on Mac |

---

## Optional — precise hot-key detection (Redis ≥ 8.6)

By default the audit is fully read-only and infers hot keys from the slow log. On Redis 8.6+ you can opt into **precise** per-key CPU/network tracking via the built-in `HOTKEYS` command:

```bash
python audit.py --hotkeys 10   # track for 10 seconds per node
```

> ⚠️ This mode is **invasive**: it runs `HOTKEYS START/STOP/RESET`, which mutates the server's tracking state, and it needs a user permitted to run `HOTKEYS` (grant `+HOTKEYS`). It is off by default — the standard audit never runs it. Nodes older than 8.6 are skipped automatically.

## Common findings

**`timeout=0`** — idle connections never close. They accumulate until `maxclients` is reached, at which point Redis refuses new connections. Fix:
```
CONFIG SET timeout 300
CONFIG SET tcp-keepalive 60
```

**`maxmemory=0`** — no memory cap. A memory leak or data burst will OOM-kill the process.

**Default user `nopass`** — any client can connect without authentication.

---

## Running the tests

The unit tests use fake Redis clients, so no live server or Docker is needed:

```bash
pip install -r requirements-dev.txt
pytest
```

---

## License

GNU Lesser General Public License v3.0 — see [LICENSE](LICENSE).
