#!/usr/bin/env bash
# Package the extension into a zip ready for the Chrome/Edge Web Store.
# Usage: ./build.sh   (run from the luma-icp-scout folder)
set -e
cd "$(dirname "$0")"
VER=$(grep -o '"version"[^,]*' manifest.json | grep -o '[0-9][0-9.]*')
OUT="../luma-icp-scout-v${VER}.zip"
rm -f "$OUT"
# Only the files the extension actually needs (no server/, no docs, no cruft).
zip -r "$OUT" \
  manifest.json background.js \
  sidepanel.html sidepanel.css sidepanel.js \
  lib dashboard pdf.html pdf.js icons \
  -x "*.DS_Store" -x "*/__pycache__/*"
echo "Built $OUT (version $VER)"
