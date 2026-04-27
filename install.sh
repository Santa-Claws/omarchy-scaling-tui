#!/usr/bin/env bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HOME/.local/bin/omarchy-scaling-tui"
mkdir -p "$HOME/.local/bin"
ln -sf "$SCRIPT_DIR/scaling_tui.py" "$TARGET"
echo "Installed: $TARGET"
echo "Run with: omarchy-scaling-tui"
