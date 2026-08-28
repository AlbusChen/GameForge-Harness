#!/bin/bash
# Path-only macOS transport for GameCraft-Bench's public screenshot.gd helper.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_PATH=""
FORWARD_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --path)
            PROJECT_PATH="$2"
            shift 2
            ;;
        --path=*)
            PROJECT_PATH="${1#--path=}"
            shift
            ;;
        *)
            FORWARD_ARGS+=("$1")
            shift
            ;;
    esac
done

if [[ -z "$PROJECT_PATH" ]]; then
    echo "macos_screenshot.sh: --path <project> is required" >&2
    exit 2
fi

ENGINE="${GODOT:-godot}"

exec "$ENGINE" \
    --path "$PROJECT_PATH" \
    --display-driver macos \
    --rendering-driver opengl3 \
    --audio-driver Dummy \
    --resolution 1280x720 \
    --script "$SCRIPT_DIR/screenshot.gd" \
    -- "${FORWARD_ARGS[@]}"
