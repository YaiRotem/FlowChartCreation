#!/usr/bin/env sh
# FlowChartCreation - manage agents (local AI assistants).  Example: ./agents.sh add "Planner"
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
if [ $# -eq 0 ]; then $PY server.py agent --help; echo; $PY server.py agent list; exit 0; fi
exec $PY server.py agent "$@"
