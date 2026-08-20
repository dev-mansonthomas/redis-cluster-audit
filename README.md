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
| **Hot Keys** *(opt-in `--hotkeys`)* | Per-key CPU / network share via `HOTKEYS` tracking (Redis ≥ 8.6) |

Recommendations now also cover: `noeviction` on a capped cache, an app connecting as the `default` user, connection concentration on one IP, monolithic-String data models, high fragmentation, latency-monitor/spikes, AOF+RDB both enabled, and hot keys.

---

## Prerequisites

| Tool | Min version |
|---|---|
| Python | 3.10+ |
| Docker Desktop | 4.x (local test fixture only) |
| redis-cli | 6.x (cluster init + `create_audit_user.sh`) |

---

## Installation

The entry scripts (`run_audit.sh`, `create_audit_user.sh`, `local-test/run.sh`) create a local `.venv` and install dependencies automatically on first run. To install manually (e.g. to run `python audit.py` directly):

```bash
python3 -m venv .venv && source .venv/bin/activate
```
```bash
pip install -r requirements.txt
```

---

## Quick start — local Docker (test fixture)

To exercise the tool against a throwaway cluster, in one command:

```bash
bash local-test/run.sh
```

This is the internal **test fixture**, not the audit entry point: it creates a `.venv`, starts a 3-shard Redis 6.2.20 cluster, provisions a read-only user, seeds bad-practice data, and runs the audit. It uses fixed test credentials and does **not** read `.env`. To audit a real server, use the root scripts below.

---

## Production usage — audit a real server

Three root scripts handle everything (each creates a `.venv` and installs deps on first run):

| Script | Purpose |
|---|---|
| `./create_audit_user.sh` | Provision the read-only audit user on every node (prompts for the password) |
| `./run_audit.sh` | Run the read-only audit → `report.html` |
| `./run_audit_hotkeys.sh [N]` | Read-only audit **plus** invasive HOTKEYS tracking (Redis ≥ 8.6) |

### Step 1 — Point `.env` at your nodes, then create the user

```bash
cp .env.example .env      # set REDIS_HOST_*/PORT_* (the node addresses)
```
```bash
./create_audit_user.sh    # prompts for the new password + admin credentials
```

`create_audit_user.sh` provisions the user on every cluster node (discovered via `CLUSTER NODES`), using `audit.py --print-acl` as the single source of truth for the grants. To do it by hand instead, run that command and apply the printed `ACL SETUSER …` on each node:

```bash
python audit.py --print-acl audit_ro YOUR_PASSWORD
```

### Step 2 — Finish configuring `.env`

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
./run_audit.sh
# → report.html
```

---

## Project structure

```
redis-cluster-audit/
├── audit.py                 ← the audit (produces report.html)
├── run_audit.sh             ← entry point: read-only audit
├── run_audit_hotkeys.sh     ← entry point: audit + invasive HOTKEYS (Redis ≥ 8.6)
├── create_audit_user.sh     ← provision the read-only user on every node
├── requirements.txt
├── .env.example             ← production config template
├── tests/                   ← unit tests (pytest, no server needed)
├── local-test/
│   └── run.sh               ← Docker integration fixture (NOT the entry point)
├── docker/
│   ├── docker-compose.yml     ← 3-node Redis 6.2.20 cluster (default fixture)
│   ├── docker-compose-8.yml   ← 3-node Redis 8.10 cluster (--hotkeys fixture)
│   └── init-cluster.sh
└── seed/
    ├── seed.py                ← loads test data (big keys, slow logs, TTL mix)
    └── hotkey_load.py         ← concentrated load on one key (for --hotkeys)
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
| `SCAN_MEMORY_SAMPLES` | 5 | `MEMORY USAGE` sampling depth (0 = exact O(N) walk — avoid on prod) |
| `SLOWLOG_MAX_ENTRIES` | 25 | Slow log entries fetched per node |
| `CONN_PER_IP_WARN` | 200 | Warn when one source IP holds more than this many connections |
| `REDIS_DOCKER_REMAP` | false | Enable only for the local Docker fixture on Mac |

---

## Optional — precise hot-key detection (Redis ≥ 8.6)

By default the audit is fully read-only and infers hot keys from the slow log. On Redis 8.6+ you can opt into **precise** per-key CPU/network tracking via the built-in `HOTKEYS` command:

```bash
./run_audit_hotkeys.sh 10   # track for 10 seconds per node
```

> ⚠️ This mode is **invasive**: it runs `HOTKEYS START/STOP/RESET`, which mutates the server's tracking state, and it needs a user permitted to run `HOTKEYS` (grant `+HOTKEYS` — `create_audit_user.sh` offers to do this). It is off by default — the standard audit never runs it. Nodes older than 8.6 are skipped automatically.

**Try it locally** against a throwaway Redis 8.10 cluster — this spins up the fixture, generates load on one key, then runs the HOTKEYS audit so the report's *Hot Keys* section is populated:

```bash
bash local-test/run.sh --hotkeys 10   # 10 = tracking seconds (default 10)
```

## Common findings

**`timeout=0`** — idle connections never close. They accumulate until `maxclients` is reached, at which point Redis refuses new connections. Fix:
```
CONFIG SET timeout 300
CONFIG SET tcp-keepalive 60
```

**`maxmemory=0`** — no memory cap. A memory leak or data burst will OOM-kill the process.

**Default user `nopass`** — any client can connect without authentication.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ERROR: Cannot connect` | Check `REDIS_HOST_*`/`REDIS_PORT_*` and that the audit user exists on every node (`./create_audit_user.sh`). For the **local fixture** only, `REDIS_DOCKER_REMAP=true` is required. |
| `STOP — the Redis user has WRITE access` | You pointed the audit at a write-capable user. Use a read-only user (`./create_audit_user.sh`). The canary write is removed immediately. |
| `pip: externally-managed-environment` (macOS/Homebrew) | Use the entry scripts (they create a `.venv`), or install inside a venv: `python3 -m venv .venv && source .venv/bin/activate`. |
| `Cannot connect to the Docker daemon` | Start Docker Desktop before `local-test/run.sh`. |
| `redis-cli: command not found` | `brew install redis` (needed for the local fixture and `create_audit_user.sh`). |
| Port `6777/6778/6779` already in use | A previous fixture is still up: `docker compose -f docker/docker-compose.yml down && docker compose -f docker/docker-compose-8.yml down`. |
| `--hotkeys` shows "Redis < 8.6 — skipped" | HOTKEYS needs Redis ≥ 8.6; the default local fixture is 6.2.20 — use `local-test/run.sh --hotkeys` (Redis 8.10) or a real 8.6+ server. |

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
