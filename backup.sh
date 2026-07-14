#!/usr/bin/env bash
# Snapshot the working tree before a patch. Keeps the last 10.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p .backups
ts=$(date +%Y%m%d-%H%M%S)
tar czf ".backups/adtrack-${ts}.tar.gz" \
    --exclude='.backups' --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.db*' --exclude='.env' \
    app static tools 2>/dev/null || true
ls -1t .backups/*.tar.gz | tail -n +11 | xargs -r rm --
echo "backup: .backups/adtrack-${ts}.tar.gz"
