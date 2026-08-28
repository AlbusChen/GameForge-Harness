#!/bin/zsh
set -euo pipefail

project_root="${0:A:h:h}"
cd "$project_root"
uv run gameforge baseline \
  --spec specs/arena_demo.yaml \
  --project unity/ArenaTemplate \
  "$@"
