#!/bin/bash
# Provision the read-only audit user on every node of a Redis cluster.
#
# Prompts for the new password (and admin credentials to connect with), then
# creates the user from `audit.py --print-acl` (the single source of truth for
# the grants) on each node discovered via CLUSTER NODES — falling back to the
# REDIS_HOST_* seeds in .env for a non-cluster server.
#
# Usage: ./create_audit_user.sh

set -e
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Python env (audit.py --print-acl imports redis-py) ────────────────────────
if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
else
    if [ ! -x .venv/bin/python ]; then
        echo "Setting up .venv..."
        python3 -m venv .venv
        .venv/bin/pip install -q -r requirements.txt
    fi
    PY=".venv/bin/python"
fi

if [ ! -f .env ]; then
    echo "ERROR: .env not found. Copy .env.example and set REDIS_HOST_*/PORT_* first:"
    echo "    cp .env.example .env"
    exit 1
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a
if [ -z "${REDIS_HOST_1:-}" ]; then
    echo "ERROR: REDIS_HOST_1 is not set in .env."
    exit 1
fi

# ── Prompts ───────────────────────────────────────────────────────────────────
read -r -p "Audit username [audit_ro]: " AUDIT_USER
if [ -z "$AUDIT_USER" ]; then AUDIT_USER="audit_ro"; fi
read -r -s -p "New password for '$AUDIT_USER': " AUDIT_PASS; echo
read -r -s -p "Confirm password: " AUDIT_PASS2; echo
if [ "$AUDIT_PASS" != "$AUDIT_PASS2" ]; then echo "Passwords do not match."; exit 1; fi
if [ -z "$AUDIT_PASS" ]; then echo "Empty password is not allowed."; exit 1; fi
read -r -p "Admin user to connect as [default]: " ADMIN_USER
if [ -z "$ADMIN_USER" ]; then ADMIN_USER="default"; fi
read -r -s -p "Admin password (leave blank if none): " ADMIN_PASS; echo
read -r -p "Also grant +HOTKEYS (for run_audit_hotkeys.sh)? [y/N]: " GRANT_HK

# ── redis-cli admin auth args ─────────────────────────────────────────────────
AUTH=()
if [ "$ADMIN_USER" != "default" ]; then AUTH+=(--user "$ADMIN_USER"); fi
if [ -n "$ADMIN_PASS" ]; then AUTH+=(--pass "$ADMIN_PASS" --no-auth-warning); fi

SEED_HOST="$REDIS_HOST_1"
SEED_PORT="${REDIS_PORT_1:-6379}"

# ── Discover all cluster nodes (fall back to the .env seed list) ──────────────
NODES=()
CLUSTER_OUT="$(redis-cli -h "$SEED_HOST" -p "$SEED_PORT" "${AUTH[@]}" CLUSTER NODES 2>/dev/null || true)"
if [ -n "$CLUSTER_OUT" ]; then
    while IFS= read -r line; do
        addr="$(printf '%s' "$line" | awk '{print $2}' | cut -d'@' -f1)"
        if [ -n "$addr" ]; then NODES+=("$addr"); fi
    done <<< "$CLUSTER_OUT"
fi
if [ ${#NODES[@]} -eq 0 ]; then
    for pair in \
        "${REDIS_HOST_1:-}:${REDIS_PORT_1:-6379}" \
        "${REDIS_HOST_2:-}:${REDIS_PORT_2:-6379}" \
        "${REDIS_HOST_3:-}:${REDIS_PORT_3:-6379}"; do
        host="${pair%%:*}"
        if [ -n "$host" ]; then NODES+=("$pair"); fi
    done
fi
echo "Target nodes: ${NODES[*]}"

# ── Create the user on each node (grants from audit.py --print-acl) ───────────
ACL_CMD="$("$PY" audit.py --print-acl "$AUDIT_USER" "$AUDIT_PASS")"

FAILED=0
for node in "${NODES[@]}"; do
    host="${node%:*}"; port="${node##*:}"
    printf '  %s ... ' "$node"
    if echo "$ACL_CMD" | redis-cli -h "$host" -p "$port" "${AUTH[@]}" >/dev/null 2>&1; then
        case "$GRANT_HK" in
            [Yy]*) redis-cli -h "$host" -p "$port" "${AUTH[@]}" \
                       ACL SETUSER "$AUDIT_USER" +HOTKEYS >/dev/null 2>&1 || true ;;
        esac
        redis-cli -h "$host" -p "$port" "${AUTH[@]}" ACL SAVE >/dev/null 2>&1 || true
        echo "OK"
    else
        echo "FAILED (check admin credentials / connectivity)"
        FAILED=1
    fi
done

echo ""
if [ "$FAILED" -eq 0 ]; then
    echo "Done. Now set in .env:"
    echo "    REDIS_USERNAME=$AUDIT_USER"
    echo "    REDIS_PASSWORD=<the password you just entered>"
    echo "Then run:  ./run_audit.sh"
else
    echo "Some nodes failed — fix connectivity/credentials and re-run."
    exit 1
fi
