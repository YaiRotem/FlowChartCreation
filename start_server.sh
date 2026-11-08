#!/usr/bin/env sh
# FlowChartCreation - share diagrams on the local network (macOS / Linux)
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
  exec python3 server.py --open "$@"
elif command -v python >/dev/null 2>&1; then
  exec python server.py --open "$@"
else
  echo "Python 3 was not found. Install it from https://www.python.org/downloads/"
  exit 1
fi
