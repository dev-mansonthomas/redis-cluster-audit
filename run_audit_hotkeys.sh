#!/bin/bash
# Entry point — read-only audit PLUS invasive HOTKEYS hot-key tracking.
#
#   ./run_audit_hotkeys.sh [SECONDS]     (default 10)
#
# Requires Redis >= 8.6 and a user granted +HOTKEYS (see ./create_audit_user.sh).
# HOTKEYS mutates the server's tracking state (START/STOP/RESET); it does not
# modify your data. Thin wrapper around run_audit.sh.

set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/run_audit.sh" --hotkeys "${1:-10}"
