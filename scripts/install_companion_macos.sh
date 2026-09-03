#!/bin/zsh
set -euo pipefail

PLUGIN_SOURCE="${0:A:h:h}"
DEFAULT_DATA_DIR="$(python3 -c 'from pathlib import Path; p=Path.home()/".codex"/"plugins"/"data"/"doneguard-personal"; print(p if p.exists() else Path.home()/".codex"/"doneguard-data")')"
DATA_DIR="${PLUGIN_DATA:-${DONEGUARD_DATA:-$DEFAULT_DATA_DIR}}"

if [[ "${1:-}" == "--data-dir" ]]; then
  if [[ -z "${2:-}" ]]; then
    print -u2 "--data-dir requires a path"
    exit 2
  fi
  DATA_DIR="$2"
fi

STAGING_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGING_DIR"' EXIT
APP_NAME="DoneGuard Companion.app"
APP_PATH="$STAGING_DIR/$APP_NAME"
TARGET_PATH="$DATA_DIR/$APP_NAME"

mkdir -p "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources" "$DATA_DIR"
swiftc -parse-as-library \
  "$PLUGIN_SOURCE/companion/Sources/DoneGuardCompanion.swift" \
  -framework AppKit -framework SwiftUI \
  -o "$APP_PATH/Contents/MacOS/DoneGuardCompanion"
cp "$PLUGIN_SOURCE/companion/Support/Info.plist" "$APP_PATH/Contents/Info.plist"
cp "$PLUGIN_SOURCE/companion/Assets/mascot-success.png" "$APP_PATH/Contents/Resources/mascot-success.png"
cp "$PLUGIN_SOURCE/companion/Assets/mascot-issue.png" "$APP_PATH/Contents/Resources/mascot-issue.png"

if [[ -e "$TARGET_PATH" ]]; then
  BACKUP_PATH="$DATA_DIR/DoneGuard Companion.previous.app"
  if [[ -e "$BACKUP_PATH" ]]; then
    rm -rf "$BACKUP_PATH"
  fi
  mv "$TARGET_PATH" "$BACKUP_PATH"
fi
mv "$APP_PATH" "$TARGET_PATH"
xattr -dr com.apple.quarantine "$TARGET_PATH" 2>/dev/null || true
print "$TARGET_PATH"
