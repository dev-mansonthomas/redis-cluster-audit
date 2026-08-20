#!/bin/bash
# INTERNAL TEST FIXTURE — not the audit entry point.
# Spins up a throwaway Redis cluster in Docker, seeds bad-practice data, and
# runs the audit against it to exercise the tool end to end.
#
# For auditing a REAL server use the root scripts instead:
#   ./run_audit.sh   ./run_audit_hotkeys.sh   ./create_audit_user.sh
#
#   bash local-test/run.sh                 Redis 6.2.20 cluster, read-only audit.
#   bash local-test/run.sh --hotkeys [N]   Redis 8.10 cluster; generate load on one
#                                          key, then run the INVASIVE HOTKEYS audit
#                                          for N seconds (default 10).
#
# The fixtures use fixed test credentials and do NOT read .env. Set PYTHON to
# override the interpreter; otherwise a local .venv is created.
#
# Usage: bash local-test/run.sh [--hotkeys [SECONDS]]

set -e

# Operate from the repo root regardless of where this is called from.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Interpreter: honour $PYTHON, else use/create a local .venv.
if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
else
    if [ ! -x .venv/bin/python ]; then
        echo "Creating .venv..."
        python3 -m venv .venv
    fi
    PY=".venv/bin/python"
fi

AUDIT_USER="audit_ro"
AUDIT_PASS="audit123"

# ── Mode selection ────────────────────────────────────────────────────────────
MODE="default"
HOTKEYS_SECONDS=10
COMPOSE_FILE="docker/docker-compose.yml"
if [ "${1:-}" = "--hotkeys" ] || [ "${1:-}" = "hotkeys" ]; then
    MODE="hotkeys"
    COMPOSE_FILE="docker/docker-compose-8.yml"
    if [ -n "${2:-}" ]; then HOTKEYS_SECONDS="$2"; fi
fi

# Pin every connection to the local Docker cluster, independent of any .env.
DOCKER_ENV=(
    REDIS_HOST_1=127.0.0.1 REDIS_PORT_1=6777
    REDIS_HOST_2=127.0.0.1 REDIS_PORT_2=6778
    REDIS_HOST_3=127.0.0.1 REDIS_PORT_3=6779
    REDIS_DOCKER_REMAP=true
)

echo "========================================"
echo " Redis Cluster Audit — Local Test Fixture"
echo " Mode: $MODE  ($COMPOSE_FILE)"
echo "========================================"
echo ""

# ── 1. Dependencies ──────────────────────────────────────────────────────────
echo "[1/6] Installing Python dependencies..."
"$PY" -m pip install -q -r requirements.txt
echo ""

# ── 2. Docker (tear down either fixture first, then start the chosen one) ──────
echo "[2/6] Starting Docker cluster..."
docker compose -f docker/docker-compose.yml   down >/dev/null 2>&1 || true
docker compose -f docker/docker-compose-8.yml down >/dev/null 2>&1 || true
docker compose -f "$COMPOSE_FILE" up -d
echo ""

# ── 3. Cluster init ──────────────────────────────────────────────────────────
echo "[3/6] Initialising cluster topology..."
bash docker/init-cluster.sh
echo ""

# ── 4. Read-only user (grants are audit.py's single source of truth) ──────────
echo "[4/6] Creating read-only audit user '$AUDIT_USER'..."
ACL_CMD="$("$PY" audit.py --print-acl "$AUDIT_USER" "$AUDIT_PASS")"
for port in 6777 6778 6779; do
    echo "$ACL_CMD" | redis-cli -p "$port" > /dev/null
    echo "  :$port — OK"
done
if [ "$MODE" = "hotkeys" ]; then
    # HOTKEYS is invasive and not part of the read-only grant set; add it just
    # for this test user so the --hotkeys audit can run it.
    echo "  + granting +HOTKEYS to $AUDIT_USER (invasive mode)"
    for port in 6777 6778 6779; do
        redis-cli -p "$port" ACL SETUSER "$AUDIT_USER" +HOTKEYS > /dev/null
    done
else
    # Extra bad practices for the default demo: an over-privileged app user and
    # a few failed AUTH attempts (populate the ACL LOG).
    echo "  + over-privileged app_user + failed-auth entries (demo only)"
    APP_ACL="ACL SETUSER app_user on >app_pass ~* +@all"
    for port in 6777 6778 6779; do echo "$APP_ACL" | redis-cli -p "$port" > /dev/null; done
    for _ in 1 2 3 4 5 6; do
        redis-cli -p 6777 --user app_user --pass wrongpass --no-auth-warning ping > /dev/null 2>&1 || true
    done
fi
echo ""

# ── 5. Seed data (as the write-capable default user, NOT audit_ro) ────────────
echo "[5/6] Seeding test data..."
env "${DOCKER_ENV[@]}" REDIS_USERNAME= REDIS_PASSWORD= "$PY" seed/seed.py
echo ""

# ── 6. Audit ──────────────────────────────────────────────────────────────────
if [ "$MODE" = "hotkeys" ]; then
    echo "[6/6] Generating hot-key load, then running HOTKEYS audit (${HOTKEYS_SECONDS}s)..."
    env "${DOCKER_ENV[@]}" REDIS_USERNAME= REDIS_PASSWORD= \
        "$PY" seed/hotkey_load.py "$((HOTKEYS_SECONDS + 20))" &
    LOAD_PID=$!
    env "${DOCKER_ENV[@]}" REDIS_USERNAME="$AUDIT_USER" REDIS_PASSWORD="$AUDIT_PASS" \
        "$PY" audit.py --hotkeys "$HOTKEYS_SECONDS" || true
    kill "$LOAD_PID" 2>/dev/null || true
    wait "$LOAD_PID" 2>/dev/null || true
else
    echo "[6/6] Running audit..."
    env "${DOCKER_ENV[@]}" REDIS_USERNAME="$AUDIT_USER" REDIS_PASSWORD="$AUDIT_PASS" \
        "$PY" audit.py
fi
echo ""

echo "========================================"
echo " Done. Open report.html in your browser."
echo "========================================"

# Try to open automatically on Mac
if command -v open &>/dev/null; then
    open report.html
fi
