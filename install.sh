#!/usr/bin/env bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HOME/.local/bin/omarchy-scaling-tui"
mkdir -p "$HOME/.local/bin"
ln -sf "$SCRIPT_DIR/scaling_tui.py" "$TARGET"
install -Dm644 "$SCRIPT_DIR/scaling-tui.desktop" \
  "$HOME/.local/share/applications/scaling-tui.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Installed: $TARGET"
echo "Run with: omarchy-scaling-tui"
