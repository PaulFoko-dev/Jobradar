@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Lance JobRadar sous Windows et synchronise automatiquement ses dependances.

cd /d "%~dp0" || goto :project_error
set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
set "REQUIREMENTS_FILE=%CD%\requirements.txt"
set "REQUIREMENTS_STAMP=%CD%\.venv\.jobradar-requirements.sha256"

if not exist "%REQUIREMENTS_FILE%" goto :requirements_error

set "BOOTSTRAP_PYTHON="
py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 set "BOOTSTRAP_PYTHON=py -3"
if not defined BOOTSTRAP_PYTHON (
  python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
  if not errorlevel 1 set "BOOTSTRAP_PYTHON=python"
)
if not defined BOOTSTRAP_PYTHON goto :python_error

if not exist "%VENV_PYTHON%" (
  echo Creation de l'environnement Python...
  %BOOTSTRAP_PYTHON% -m venv "%CD%\.venv"
  if errorlevel 1 goto :venv_error
)

"%VENV_PYTHON%" -c "import sys; raise SystemExit(sys.version_info < (3, 10))"
if errorlevel 1 goto :venv_version_error

set "CURRENT_HASH="
for /f "usebackq delims=" %%H in (`"%VENV_PYTHON%" -c "import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())" "%REQUIREMENTS_FILE%"`) do set "CURRENT_HASH=%%H"
if not defined CURRENT_HASH goto :hash_error

set "INSTALLED_HASH="
if exist "%REQUIREMENTS_STAMP%" set /p INSTALLED_HASH=<"%REQUIREMENTS_STAMP%"
set "NEED_INSTALL=0"
if not "%CURRENT_HASH%"=="%INSTALLED_HASH%" set "NEED_INSTALL=1"
"%VENV_PYTHON%" -m pip check >nul 2>&1
if errorlevel 1 set "NEED_INSTALL=1"
"%VENV_PYTHON%" -c "import bs4, fastapi, httpx, jobradar, jobspy, playwright, pydantic, uvicorn" >nul 2>&1
if errorlevel 1 set "NEED_INSTALL=1"

if "%NEED_INSTALL%"=="1" (
  echo Synchronisation des dependances Python...
  "%VENV_PYTHON%" -m pip install --disable-pip-version-check -r "%REQUIREMENTS_FILE%"
  if errorlevel 1 goto :pip_error
  "%VENV_PYTHON%" -m pip check
  if errorlevel 1 goto :venv_health_error
  "%VENV_PYTHON%" -c "import bs4, fastapi, httpx, jobradar, jobspy, playwright, pydantic, uvicorn"
  if errorlevel 1 goto :venv_health_error
  >"%REQUIREMENTS_STAMP%" echo %CURRENT_HASH%
)

"%VENV_PYTHON%" -c "from jobradar.capture import browser_status; raise SystemExit(0 if browser_status()['available'] else 1)" >nul 2>&1
if errorlevel 1 (
  echo Aucun navigateur compatible avec la capture des offres. Installation de Chromium Playwright...
  "%VENV_PYTHON%" -m playwright install chromium
  if errorlevel 1 echo Avertissement JobRadar : Chromium n'a pas pu etre installe ; la capture des offres fonctionnera en mode degrade.>&2
)

"%VENV_PYTHON%" -m jobradar.cli serve --open %*
exit /b %errorlevel%

:project_error
echo Erreur JobRadar : impossible d'acceder au dossier du projet.>&2
exit /b 1
:requirements_error
echo Erreur JobRadar : requirements.txt est introuvable.>&2
exit /b 1
:python_error
echo Erreur JobRadar : Python 3.10 ou plus recent est requis.>&2
exit /b 1
:venv_error
echo Erreur JobRadar : impossible de creer .venv. Verifiez que le module venv est installe.>&2
exit /b 1
:venv_version_error
echo Erreur JobRadar : .venv utilise Python ^< 3.10. Supprimez ce dossier puis relancez run.bat.>&2
exit /b 1
:hash_error
echo Erreur JobRadar : impossible de calculer l'empreinte des dependances.>&2
exit /b 1
:pip_error
echo Erreur JobRadar : installation des dependances impossible. Consultez les messages ci-dessus.>&2
exit /b 1
:venv_health_error
echo Erreur JobRadar : l'environnement Python reste incoherent apres installation.>&2
exit /b 1
