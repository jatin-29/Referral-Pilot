#!/usr/bin/env sh
# Rebuild referralpilot/ui/static/tailwind.css after editing templates (needs Node.js).
set -e
cd "$(dirname "$0")/.."
npx --yes tailwindcss@3.4.19 -c tailwind.config.js \
  -i referralpilot/ui/tailwind.input.css \
  -o referralpilot/ui/static/tailwind.css --minify
