#!/usr/bin/env bash
# One-time: GitHub → Settings → Actions → Runners → New self-hosted runner → copy token.
# Usage: RUNNER_TOKEN='...' bash scripts/install-github-runner.sh
set -euo pipefail

REPO_URL="${GITHUB_REPO_URL:-https://github.com/AnyOne76/smyvy-quality-journal}"
RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner-smyvy}"
RUNNER_VERSION="${RUNNER_VERSION:-2.321.0}"

if [[ -z "${RUNNER_TOKEN:-}" ]]; then
  echo "Set RUNNER_TOKEN from GitHub (New self-hosted runner)." >&2
  exit 1
fi

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"
if [[ ! -f ./config.sh ]]; then
  curl -fsSL -o actions-runner.tar.gz \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
  tar xzf actions-runner.tar.gz
  rm -f actions-runner.tar.gz
fi

./config.sh --url "$REPO_URL" --token "$RUNNER_TOKEN" --labels aidev --unattended
./svc.sh install
./svc.sh start
echo "Runner installed in $RUNNER_DIR"
