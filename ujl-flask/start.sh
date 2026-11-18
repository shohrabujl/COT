#!/bin/sh
# macOS / Linux: ./start.sh
cd "$(dirname "$0")" || exit 1
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -c "import flask, openpyxl" 2>/dev/null || .venv/bin/python -m pip install -q -r requirements.txt
exec .venv/bin/python run.py
