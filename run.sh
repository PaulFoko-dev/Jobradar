#!/usr/bin/env bash
# Lance JobRadar (Linux / macOS) sur http://127.0.0.1:8765 et l'ouvre dans le navigateur.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m jobradar.cli serve --open "$@"
