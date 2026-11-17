#!/bin/sh
# Cross-platform dev setup helper (Unix/macOS)
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "Setting up dev environment..."

# Backend
cd "$ROOT/backend"
if [ ! -d ".venv" ]; then
    uv venv
fi
# NOSONAR shell:S8541: pytest-bdd is a pinned source-archive dep (no wheel) and
# ibm-cos-sdk is sdist-only, so `--no-build` hard-fails the sync. `--frozen`
# locks the exact revisions, so builds are deterministic and trusted.
uv sync  # NOSONAR

# Frontend
# S6505: do not execute arbitrary install lifecycle scripts from deps.
cd "$ROOT/frontend"
pnpm install --ignore-scripts
# Run only the project's own known, required postinstall step (no third-party scripts).
node scripts/internationalized-date-patch.cjs

# Docker (optional)
if [ "$1" = "--full" ]; then
    docker compose -f "$ROOT/docker-compose.yml" up -d
fi

# Pre-commit hooks (commit-stage + CI-parity pre-push gate)
if command -v pre-commit >/dev/null 2>&1; then
    (cd "$ROOT" && pre-commit install && pre-commit install --hook-type pre-push)
else
    echo "pre-commit not found — run 'pre-commit install && pre-commit install --hook-type pre-push' from the repo root manually."
fi

echo "Done!"
