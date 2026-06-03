#!/bin/bash
# Full local test run:
#   1. Start the Docker cluster
#   2. Form the cluster topology
#   3. Create the read-only audit user
#   4. Seed test data (big keys, slow logs, TTL mix)
#   5. Run the audit → report.html
#
# Usage: bash run.sh

set -e

COMPOSE_FILE="docker/docker-compose.yml"
ENV_FILE=".env"

# ── Load .env so we can use REDIS_PASSWORD / REDIS_USERNAME ──────────────────
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found. Copy .env.example and fill in your values."
    exit 1
fi
source "$ENV_FILE"

AUDIT_USER="${REDIS_USERNAME:-audit_ro}"
AUDIT_PASS="${REDIS_PASSWORD:-audit123}"

echo "========================================"
echo " Redis Cluster Audit — Local Test Run"
echo "========================================"
echo ""

# ── 1. Docker ────────────────────────────────────────────────────────────────
echo "[1/5] Starting Docker cluster..."
docker compose -f "$COMPOSE_FILE" up -d
echo ""

# ── 2. Cluster init ──────────────────────────────────────────────────────────
echo "[2/5] Initialising cluster topology..."
bash docker/init-cluster.sh
echo ""

# ── 3. Read-only user ────────────────────────────────────────────────────────
echo "[3/5] Creating read-only audit user '$AUDIT_USER'..."
for port in 6777 6778 6779; do
    redis-cli -p "$port" ACL SETUSER "$AUDIT_USER" on ">$AUDIT_PASS" '~*' '&*' nocommands \
        +INFO +"CONFIG|GET" \
        +"SLOWLOG|GET" +"SLOWLOG|LEN" \
        +"MEMORY|USAGE" +"MEMORY|DOCTOR" +"MEMORY|STATS" \
        +"CLIENT|LIST" \
        +DBSIZE \
        +"CLUSTER|INFO" +"CLUSTER|NODES" +"CLUSTER|SLOTS" \
        +"CLUSTER|SHARDS" +"CLUSTER|KEYSLOT" +"CLUSTER|MYID" \
        +SCAN +TYPE +TTL +"OBJECT|ENCODING" +"OBJECT|IDLETIME" \
        +"ACL|LIST" +"ACL|USERS" +"ACL|CAT" +"ACL|LOG" \
        +"LATENCY|LATEST" +"LATENCY|HISTORY" \
        +PING +READONLY +COMMAND > /dev/null
    echo "  :$port — OK"
done
echo ""

# ── 4. Seed data ─────────────────────────────────────────────────────────────
echo "[4/5] Seeding test data..."
python3 seed/seed.py
echo ""

# ── 5. Audit ─────────────────────────────────────────────────────────────────
echo "[5/5] Running audit..."
python3 audit.py
echo ""

echo "========================================"
echo " Done. Open report.html in your browser."
echo "========================================"

# Try to open automatically on Mac
if command -v open &>/dev/null; then
    open report.html
fi
