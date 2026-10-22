@echo off
rem Lance JobRadar (Windows) sur http://127.0.0.1:8765 et ouvre le navigateur.
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
.venv\Scripts\python -m jobradar.cli serve --open %*
