#!/usr/bin/env bash
# Kopieert de bot-sources in de add-on map (nodig vóór lokale Supervisor-build en in CI).
# Delete-loos zodat het ook werkt op filesystems zonder verwijderrechten.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p src config
cp -r ../src/. src/
cp -r ../config/. config/
cp ../requirements.txt .
find src -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
echo "Add-on build context klaar: $(du -sh . | cut -f1)"
