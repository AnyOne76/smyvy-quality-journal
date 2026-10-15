#!/usr/bin/env bash
# One-time: GitHub → Settings → Actions → Runners → New self-hosted runner → copy token.
# Usage: RUNNER_TOKEN='...' bash scripts/install-github-runner.sh
# Upgrade old binaries: RUNNER_TOKEN='...' UPGRADE=1 bash scripts/install-github-runner.sh
set -euo pipefail

REPO_URL="${GITHUB_REPO_URL:-https://github.com/AnyOne76/smyvy-quality-journal}"
RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner-smyvy}"
# GitHub minimum (2026-02): 2.329.0+
RUNNER_VERSION="${RUNNER_VERSION:-2.338.0}"

if [[ -z "${RUNNER_TOKEN:-}" ]]; then
  echo "Set RUNNER_TOKEN from GitHub (New self-hosted runner)." >&2
  exit 1
fi

mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"

need_download=0
if [[ ! -f ./config.sh ]]; then
  need_download=1
elif [[ "${UPGRADE:-}" == "1" ]]; then
  need_download=1
  if [[ -f ./svc.sh ]] && [[ -f ./.service ]]; then
    ./svc.sh stop || true
  fi
fi

if [[ "$need_download" == "1" ]]; then
  echo "==> runner ${RUNNER_VERSION} -> ${RUNNER_DIR}"
  curl -fsSL -o actions-runner.tar.gz \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
  tar xzf actions-runner.tar.gz
  rm -f actions-runner.tar.gz
fi

if [[ -f ./run.sh ]] && [[ -f ./.runner ]]; then
  echo "Runner already configured; restarting service."
  ./svc.sh install 2>/dev/null || true
  ./svc.sh start
  exit 0
fi

./config.sh --url "$REPO_URL" --token "$RUNNER_TOKEN" --labels aidev --unattended
./svc.sh install
./svc.sh start
echo "Runner installed in $RUNNER_DIR (v${RUNNER_VERSION})"
