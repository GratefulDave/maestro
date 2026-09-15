#!/bin/sh
# Herdr's server may run with a PATH that has no `node` (launchd). Resolve one
# that has node:sqlite (Node >= 22.5), then run bin/lanes.js with the same args.
for candidate in "$MAESTRO_LANES_NODE" "$(command -v node 2>/dev/null)" \
  "$HOME"/.local/share/mise/installs/node/*/bin/node /opt/homebrew/bin/node /usr/local/bin/node; do
  [ -n "$candidate" ] && [ -x "$candidate" ] || continue
  if "$candidate" -e 'require("node:sqlite")' >/dev/null 2>&1; then
    exec "$candidate" "$(dirname "$0")/lanes.js" "$@"
  fi
done
echo "maestro-lanes: no node with node:sqlite found (set MAESTRO_LANES_NODE)" >&2
exit 127
