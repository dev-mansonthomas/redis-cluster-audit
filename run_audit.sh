#!/bin/bash
# Entry point — run the read-only Redis audit against the servers in .env.
#
#   ./run_audit.sh                  read-only audit  -> report.html
#   ./run_audit.sh --hotkeys 10     also run invasive HOTKEYS tracking (Redis >= 8.6)
#
# Sets up a local .venv, installs dependencies, validates .env, then runs the
# audit. Extra arguments are passed through to audit.py.

set -e
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
"$PY" audit.py "$@"
echo "Report: $(pwd)/report.html"
