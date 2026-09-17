#!/usr/bin/env bash
# Paper-mode launcher with the macOS sleep-prevention wrapper.
#
# Closing the laptop lid (or letting it sleep) stops ExitMonitor, which is
# what enforces every stop-loss and take-profit. Without `caffeinate`,
# stops silently stop existing the moment the machine sleeps. This script
# is the mandatory way to run unattended paper-mode sessions on macOS.
set -euo pipefail

cd "$(dirname "$0")/.."

if command -v caffeinate >/dev/null 2>&1; then
    exec caffeinate -i python3 run.py "$@"
else
    echo "WARNING: caffeinate not found (not on macOS?). Stops rely on this process" >&2
    echo "         never sleeping. Ensure your OS power settings won't suspend it." >&2
    exec python3 run.py "$@"
fi
