#!/bin/bash
# Full local test run against the built-in Docker test cluster:
#   1. Install Python dependencies
#   2. Start the Docker cluster
#   3. Form the cluster topology
#   4. Create the read-only audit user (grants come from audit.py — one source)
#   5. Seed test data as the write-capable default user
#   6. Run the audit as the read-only user -> report.html
#
# The Docker cluster is a self-contained TEST FIXTURE for exercising the tool.
# It uses its own fixed credentials and does NOT read .env (which is reserved
# for auditing real, external Redis servers). Never use these creds in prod.
#
# Usage: bash run.sh

set -e

COMPOSE_FILE="docker/docker-compose.yml"

# Fixed test-fixture credentials (local Docker only).
AUDIT_USER="audit_ro"
AUDIT_PASS="audit123"

# Pin every connection to the local Docker cluster, independent of any .env.
DOCKER_ENV=(
    REDIS_HOST_1=127.0.0.1 REDIS_PORT_1=6777
    REDIS_HOST_2=127.0.0.1 REDIS_PORT_2=6778
    REDIS_HOST_3=127.0.0.1 REDIS_PORT_3=6779
    REDIS_DOCKER_REMAP=true
)

echo "========================================"
echo " Redis Cluster Audit — Local Test Run"
echo "========================================"
echo ""

# ── 1. Dependencies ──────────────────────────────────────────────────────────
echo "[1/6] Installing Python dependencies..."
python3 -m pip install -q -r requirements.txt
echo ""

# ── 2. Docker ──────────────────────────────────────────────────────────────
echo "[2/6] Starting Docker cluster..."
docker compose -f "$COMPOSE_FILE" up -d
echo ""

# ── 3. Cluster init ──────────────────────────────────────────────────────────
echo "[3/6] Initialising cluster topology..."
bash docker/init-cluster.sh
echo ""

# ── 4. Read-only user (grants are audit.py's single source of truth) ──────────
echo "[4/6] Creating read-only audit user '$AUDIT_USER'..."
# Pipe the ACL line to redis-cli via stdin so redis-cli tokenizes it (handles
# >password, ~*, &* correctly) — never rely on the shell to split the args.
ACL_CMD="$(python3 audit.py --print-acl "$AUDIT_USER" "$AUDIT_PASS")"
for port in 6777 6778 6779; do
    echo "$ACL_CMD" | redis-cli -p "$port" > /dev/null
    echo "  :$port — OK"
done
echo ""

# ── 5. Seed data (as the write-capable default user, NOT audit_ro) ────────────
echo "[5/6] Seeding test data..."
env "${DOCKER_ENV[@]}" REDIS_USERNAME= REDIS_PASSWORD= python3 seed/seed.py
echo ""

# ── 6. Audit (as the read-only user) ──────────────────────────────────────────
echo "[6/6] Running audit..."
env "${DOCKER_ENV[@]}" REDIS_USERNAME="$AUDIT_USER" REDIS_PASSWORD="$AUDIT_PASS" python3 audit.py
echo ""

echo "========================================"
echo " Done. Open report.html in your browser."
echo "========================================"

# Try to open automatically on Mac
if command -v open &>/dev/null; then
    open report.html
fi
