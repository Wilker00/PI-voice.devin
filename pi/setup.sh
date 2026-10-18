#!/usr/bin/env bash
# One-time setup on the Raspberry Pi: venv + dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

cat <<'EOF'

Setup complete. Start the app with:

  ./start.sh            # listens on 0.0.0.0:${PORT:-3000}

Then point nginx at it — see docs/SETUP_PI.md for the site block
(WebSocket needs the Upgrade headers or /ws will 404/fail).
EOF
