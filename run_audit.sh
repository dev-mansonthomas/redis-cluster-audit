#!/usr/bin/env bash
# Entry point — run the read-only Redis audit against the servers in .env.
#
#   ./run_audit.sh                  read-only audit  -> report.html
#   ./run_audit.sh --hotkeys 10     also run invasive HOTKEYS tracking (Redis >= 8.6)
#
# Sets up a local .venv, installs dependencies, validates .env, then runs the
# audit. Extra arguments are passed through to audit.py.

# Requires bash >= 5 (macOS: Homebrew bash, `brew install bash`; /bin/bash there is 3.2). POSIX-only
# syntax up to here, so an older or non-bash shell reaches this check and stops before doing anything.
if [ -z "${BASH_VERSION:-}" ] || [ "${BASH_VERSION%%.*}" -lt 5 ]; then
    echo "ERROR: bash >= 5 required (found: ${BASH_VERSION:-not bash}). On macOS: brew install bash" >&2
    exit 1
fi

set -e
# THROWAWAY: unquoted expansion, shellcheck SC2086 must fail the lint job
NEGATIVE_CHECK="a b"; echo $NEGATIVE_CHECK >/dev/null
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Python environment (honour $PYTHON, else create a local .venv) ────────────
if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
else
    if [ ! -x .venv/bin/python ]; then
        echo "Setting up .venv and installing dependencies..."
        python3 -m venv .venv
        .venv/bin/pip install -q --upgrade pip
        .venv/bin/pip install -q -r requirements.txt
    fi
    PY=".venv/bin/python"
fi

# ── .env checks ───────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo "ERROR: .env not found."
    echo "  cp .env.example .env    # then fill in your hosts and credentials"
    echo "  ./create_audit_user.sh  # to provision a read-only user on each node"
    exit 1
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a

if [ -z "${REDIS_HOST_1:-}" ]; then
    echo "ERROR: REDIS_HOST_1 is not set in .env — no server to audit. See .env.example."
    exit 1
fi
if [ -z "${REDIS_USERNAME:-}" ]; then
    echo "WARNING: REDIS_USERNAME is empty — the audit will connect as the 'default' user."
    echo "         Provision a dedicated read-only user with ./create_audit_user.sh."
fi
if [ -z "${REDIS_PASSWORD:-}" ]; then
    echo "WARNING: REDIS_PASSWORD is empty (no authentication)."
fi

echo "Auditing ${REDIS_HOST_1}:${REDIS_PORT_1:-6379} as '${REDIS_USERNAME:-default}'..."
# audit.py prints "Report written to: <abs path>" itself, only when it actually wrote one — so the
# shell doesn't re-derive that from argv (info-only modes like --print-acl write no report).
"$PY" audit.py "$@"
