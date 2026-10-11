#!/bin/sh
# Light Bridge for macOS / Linux.  Usage: ./start-mac-linux.sh [--demo]
cd "$(dirname "$0")" || exit 1
if [ -f .venv/ready.txt ] && ! .venv/bin/python -c "import bleak" 2>/dev/null; then
  rm -rf .venv          # Python changed since the first start: prepare again
fi
if [ ! -f .venv/ready.txt ]; then
  echo "First start: getting everything ready…"
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt && echo ready > .venv/ready.txt || { echo "Setup failed."; exit 1; }
fi
exec .venv/bin/python -m light_bridge --open-browser "$@"
