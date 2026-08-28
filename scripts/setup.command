#!/bin/zsh
set -euo pipefail

project_root="${0:A:h:h}"
cd "$project_root"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it first with: brew install uv"
  exit 1
fi

uv sync
echo "Python environment is ready."
