#!/bin/zsh
set -euo pipefail

project_root="${0:A:h:h}"
cd "$project_root"
uv run gameforge benchmark \
  --mode repair-demo \
  --runs 20 \
  --spec specs/arena_demo.yaml \
  --project unity/ArenaTemplate \
  "$@"
