#!/usr/bin/env bash
# Регистрация runner: только обычный пользователь (devuser), без sudo.
# GitHub → Settings → Actions → Runners → New self-hosted runner → token.
#
#   RUNNER_TOKEN='...' bash scripts/install-github-runner.sh
#   RUNNER_TOKEN='...' UPGRADE=1 bash scripts/install-github-runner.sh
#   RUNNER_TOKEN='...' RECONFIGURE=1 bash scripts/install-github-runner.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Не запускайте этот скрипт через sudo. Выполните от пользователя devuser." >&2
  exit 1
fi

REPO_URL="${GITHUB_REPO_URL:-https://github.com/AnyOne76/smyvy-quality-journal}"
RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner-smyvy}"
RUNNER_VERSION="${RUNNER_VERSION:-2.338.0}"
UNIT_NAME="${RUNNER_UNIT:-actions-runner-smyvy.service}"
UNIT_DST="$HOME/.config/systemd/user/$UNIT_NAME"

if [[ -z "${RUNNER_TOKEN:-}" ]] && [[ ! -f "$RUNNER_DIR/.runner" ]] && [[ "${RECONFIGURE:-}" == "1" ]]; then
  echo "Задайте RUNNER_TOKEN (GitHub → Actions → Runners → New self-hosted runner)." >&2
  exit 1
fi

mkdir -p "$RUNNER_DIR" "$HOME/.config/systemd/user"
cd "$RUNNER_DIR"

need_download=0
if [[ ! -f ./config.sh ]]; then
  need_download=1
elif [[ "${UPGRADE:-}" == "1" ]]; then
  need_download=1
  systemctl --user stop "$UNIT_NAME" 2>/dev/null || true
fi

if [[ "$need_download" == "1" ]]; then
  echo "==> runner ${RUNNER_VERSION} -> ${RUNNER_DIR}"
  curl -fsSL -o actions-runner.tar.gz \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
  tar xzf actions-runner.tar.gz
  rm -f actions-runner.tar.gz
fi

if [[ "${RECONFIGURE:-}" == "1" ]] && [[ -f ./.runner ]]; then
  systemctl --user stop "$UNIT_NAME" 2>/dev/null || true
  ./config.sh remove --token "$RUNNER_TOKEN" || true
fi

if [[ ! -f ./.runner ]]; then
  ./config.sh --url "$REPO_URL" --token "$RUNNER_TOKEN" --labels aidev --unattended
else
  echo "==> runner уже зарегистрирован в ${RUNNER_DIR} (см. .runner)"
fi

# User systemd — без sudo (в отличие от ./svc.sh)
sed "s|%h|$HOME|g" "$REPO_ROOT/deploy/actions-runner-smyvy.service" > "$UNIT_DST"
systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
systemctl --user restart "$UNIT_NAME"
loginctl enable-linger "$USER" 2>/dev/null || true

echo "==> $(systemctl --user is-active "$UNIT_NAME") — $UNIT_NAME"
echo "Проверка: GitHub → Settings → Actions → Runners (Idle, label aidev)"
