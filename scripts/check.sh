#!/usr/bin/env bash
# Runs ruff, mypy and pytest. Uses the project venv when there is one.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -x .venv/Scripts/python.exe ]; then
    PY=.venv/Scripts/python.exe
elif [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
else
    PY=python
fi

echo "== ruff"
"$PY" -m ruff check .
echo "== mypy"
"$PY" -m mypy
echo "== pytest"
"$PY" -m pytest -q
