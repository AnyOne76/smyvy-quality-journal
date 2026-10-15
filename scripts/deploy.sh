#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${SMYVY_APP_DIR:-/home/devuser/projects/smyvy-quality-journal}"
HEALTH_URL="${SMYVY_HEALTH_URL:-http://10.0.5.173:8020/input}"
COMPOSE="${COMPOSE:-docker compose}"

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

echo "==> docker compose up"
systemctl --user stop smyvy-app.service 2>/dev/null || true
systemctl --user disable smyvy-app.service 2>/dev/null || true
if ss -tlnp 2>/dev/null | grep -q ':8020'; then
  echo "==> освобождаем порт 8020"
  fuser -k 8020/tcp 2>/dev/null || true
  sleep 1
fi

$COMPOSE build --pull
$COMPOSE up -d --remove-orphans

echo "==> health $HEALTH_URL"
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf -o /dev/null "$HEALTH_URL"; then
    $COMPOSE ps
    echo "==> ok at $(git log -1 --oneline)"
    exit 0
  fi
  sleep 2
done

echo "health check failed: $HEALTH_URL" >&2
$COMPOSE logs --tail=40
exit 1
