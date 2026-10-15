#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${SMYVY_APP_DIR:-/home/devuser/projects/smyvy-quality-journal}"
HEALTH_URL="${SMYVY_HEALTH_URL:-http://10.0.5.173:8020/input}"
SERVICE="${SMYVY_SERVICE:-smyvy-app.service}"

cd "$APP_DIR"

echo "==> deploy: $(pwd)"
git fetch origin main
git merge --ff-only origin/main

echo "==> compile check"
python3 -m py_compile app_server.py parser.py server.py

echo "==> tests (abort deploy on failure)"
if [[ -x "$APP_DIR/.venv/bin/pytest" ]]; then
  "$APP_DIR/.venv/bin/pytest" -q
elif command -v pytest >/dev/null 2>&1; then
  pytest -q
else
  python3 -m pytest -q
fi

echo "==> restart $SERVICE"
systemctl --user restart "$SERVICE"

echo "==> health $HEALTH_URL"
for i in 1 2 3 4 5; do
  if curl -sf -o /dev/null "$HEALTH_URL"; then
    echo "==> ok at $(git log -1 --oneline)"
    exit 0
  fi
  sleep 1
done

echo "health check failed: $HEALTH_URL" >&2
exit 1
