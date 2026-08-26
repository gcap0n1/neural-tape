#!/usr/bin/env bash
# rollback_to_v22.sh — Ripristina il timer v2.2 e disabilita v3
#
# Usage: sudo -u <user> bash tools/rollback_to_v22.sh
# (non serve sudo, è --user systemd)

set -euo pipefail

echo "[rollback] Re-enabling NeuralTape v2.2 cron..."

systemctl --user unmask neural-tape-v22.timer 2>/dev/null || true
systemctl --user start neural-tape-v22.timer
systemctl --user enable neural-tape-v22.timer

echo "[rollback] v2.2 timer restarted."

# Disable v3 explicitly (remove env override)
if [ -f /etc/systemd/system/neural-tape-v3.service ]; then
    systemctl --user stop neural-tape-v3.service 2>/dev/null || true
    systemctl --user disable neural-tape-v3.service 2>/dev/null || true
fi

echo "[rollback] v3 disabled. v2.2 is the active pipeline."
echo "[rollback] To verify: systemctl --user status neural-tape-v22.timer"
