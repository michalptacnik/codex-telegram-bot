#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
EXT_DIR="$ROOT_DIR/chrome-extension"
OUT_DIR="$ROOT_DIR/chrome-extension/dist"
OUT_FILE="$OUT_DIR/agenthq-chrome-bridge.zip"

mkdir -p "$OUT_DIR"
rm -f "$OUT_FILE"

cd "$EXT_DIR"
zip -r "$OUT_FILE" manifest.json background.js popup.html popup.js popup.css README.md >/dev/null

echo "Built: $OUT_FILE"
