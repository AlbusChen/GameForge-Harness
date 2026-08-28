#!/bin/zsh
set -euo pipefail

project_root="${0:A:h:h}"
cd "$project_root"
uv run gameforge doctor "$@"
