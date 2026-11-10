#!/bin/bash
# Provision the read-only audit user on every node of a Redis cluster.
#
# Prompts for the new password (and admin credentials to connect with), then
# creates the user from `audit.py --print-acl-grants` (the single source of truth
# for the grants) on each node discovered via CLUSTER NODES — falling back to the
# REDIS_HOST_* seeds in .env for a non-cluster server.
#
# Usage: ./create_audit_user.sh

set -e
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Python env (audit.py --print-acl-grants imports redis-py) ──────────────
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
# The username goes into a redis-cli stdin command line (see below), so whitespace or quotes would be
# parsed as extra ACL rules (e.g. `x nopass +@all`). Allow only a conservative character set.
case "$AUDIT_USER" in
    -*|*[!A-Za-z0-9._-]*)
        echo "Invalid username '$AUDIT_USER': use only letters, digits, '.', '_' or '-' (not leading '-')."
        exit 1 ;;
esac
read -r -s -p "New password for '$AUDIT_USER': " AUDIT_PASS; echo
read -r -s -p "Confirm password: " AUDIT_PASS2; echo
if [ "$AUDIT_PASS" != "$AUDIT_PASS2" ]; then echo "Passwords do not match."; exit 1; fi
if [ -z "$AUDIT_PASS" ]; then echo "Empty password is not allowed."; exit 1; fi
read -r -p "Admin user to connect as [default]: " ADMIN_USER
if [ -z "$ADMIN_USER" ]; then ADMIN_USER="default"; fi
read -r -s -p "Admin password (leave blank if none): " ADMIN_PASS; echo
read -r -p "Also grant +HOTKEYS (for run_audit_hotkeys.sh)? [y/N]: " GRANT_HK

# ── Secrets never go on a redis-cli command line (it is visible in `ps`) ──────
# The audit password is hashed locally and sent as `#<sha256>`; the plaintext is piped to Python via
# the `printf` builtin (no argv), and dropped right after.
AUDIT_HASH="$(printf '%s' "$AUDIT_PASS" | "$PY" -c \
    'import hashlib, sys; sys.stdout.write(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"
unset AUDIT_PASS AUDIT_PASS2
case "$AUDIT_HASH" in
    *[!0-9a-f]*|'') echo "ERROR: could not hash the password."; exit 1 ;;
esac
if [ ${#AUDIT_HASH} -ne 64 ]; then echo "ERROR: could not hash the password."; exit 1; fi

# Admin auth: the password goes through REDISCLI_AUTH, set only in redis-cli's own environment. A blank
# admin password means "no auth", so an REDISCLI_AUTH inherited from the operator's shell is dropped.
unset REDISCLI_AUTH
AUTH=()
if [ "$ADMIN_USER" != "default" ]; then AUTH+=(--user "$ADMIN_USER"); fi
rcli() {
    if [ -n "$ADMIN_PASS" ]; then
        REDISCLI_AUTH="$ADMIN_PASS" redis-cli "${AUTH[@]}" "$@"
    else
        redis-cli "${AUTH[@]}" "$@"
    fi
}

SEED_HOST="$REDIS_HOST_1"
SEED_PORT="${REDIS_PORT_1:-6379}"

# ── Discover all cluster nodes (fall back to the .env seed list) ──────────────
NODES=()
CLUSTER_OUT="$(rcli -h "$SEED_HOST" -p "$SEED_PORT" CLUSTER NODES 2>/dev/null || true)"
if [ -n "$CLUSTER_OUT" ]; then
    while IFS= read -r line; do
        addr="$(printf '%s' "$line" | awk '{print $2}' | cut -d'@' -f1)"
        flags="$(printf '%s' "$line" | awk '{print $3}')"
        # Skip nodes that aren't reachable targets: confirmed-failed / no-address / still-handshaking,
        # or a bogus ':0' / empty address — otherwise `redis-cli -h '' -p 0` fails the whole run.
        # Match whole comma-delimited flag tokens: a bare *fail* would also drop healthy nodes flagged
        # `nofailover`, and `fail?` (PFAIL — merely suspected) is still reachable so it is kept.
        case ",$flags," in *,fail,*|*,noaddr,*|*,handshake,*) continue ;; esac
        # A non-cluster server answers with an error line at exit 0 ("ERR This instance has cluster
        # support disabled"): only take lines whose address is a real `host:port` with a numeric port.
        case "$addr" in *:*) ;; *) continue ;; esac
        h="${addr%:*}"; p="${addr##*:}"
        case "$p" in ''|*[!0-9]*|0) continue ;; esac
        if [ -n "$h" ]; then NODES+=("$addr"); fi
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

# ── Create the user on each node ──────────────────────────────────────────────
# Grants come from audit.py (single source of truth) as password-free tokens. The command is sent on
# redis-cli's STDIN — `ACL SETUSER <user> on #<sha256> <grants>` — so neither the password nor its
# hash appears in argv. Every token is quote/space-free (validated username, hex hash, fixed grants),
# so redis-cli's line splitting is unambiguous. We CHECK the reply is OK, because redis-cli reading a
# command from stdin exits 0 even when the reply is a Redis error.
GRANTS="$("$PY" audit.py --print-acl-grants)"

FAILED=0
for node in "${NODES[@]}"; do
    host="${node%:*}"; port="${node##*:}"
    printf '  %s ... ' "$node"
    # Run inside an `if` so `set -e` is suspended: redis-cli exits non-zero on a refused connection,
    # and a bare `reply="$(…)"` would abort the whole run at the first unreachable node instead of
    # recording it as FAILED and carrying on with the remaining nodes.
    reply=""
    if reply="$(printf 'ACL SETUSER %s on #%s %s\n' "$AUDIT_USER" "$AUDIT_HASH" "$GRANTS" \
                    | rcli -h "$host" -p "$port" 2>&1)" \
       && [ "$reply" = "OK" ]; then
        case "$GRANT_HK" in
            [Yy]*) rcli -h "$host" -p "$port" ACL SETUSER "$AUDIT_USER" +HOTKEYS >/dev/null 2>&1 || true ;;
        esac
        rcli -h "$host" -p "$port" ACL SAVE >/dev/null 2>&1 || true
        echo "OK"
    else
        echo "FAILED (${reply:-check admin credentials / connectivity})"
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
