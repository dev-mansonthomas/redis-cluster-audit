#!/usr/bin/env bash
# Entry point — read-only audit PLUS invasive HOTKEYS hot-key tracking.
#
#   ./run_audit_hotkeys.sh [SECONDS]     (default 10)
#
# Requires Redis >= 8.6 and a user granted +HOTKEYS (see ./create_audit_user.sh).
# HOTKEYS mutates the server's tracking state (START/STOP/RESET); it does not
# modify your data. Thin wrapper around run_audit.sh.

# Requires bash >= 5 (macOS: Homebrew bash, `brew install bash`; /bin/bash there is 3.2). POSIX-only
# syntax up to here, so an older or non-bash shell reaches this check and stops before doing anything.
if [ -z "${BASH_VERSION:-}" ] || [ "${BASH_VERSION%%.*}" -lt 5 ]; then
    echo "ERROR: bash >= 5 required (found: ${BASH_VERSION:-not bash}). On macOS: brew install bash" >&2
    exit 1
fi

set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/run_audit.sh" --hotkeys "${1:-10}"
