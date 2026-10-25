#!/usr/bin/env bash
# Lance JobRadar (Linux / macOS) sur http://127.0.0.1:8765.
# L'environnement virtuel et ses dependances sont synchronises automatiquement.
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly VENV_DIR="$SCRIPT_DIR/.venv"
readonly VENV_PYTHON="$VENV_DIR/bin/python"
readonly REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"
readonly REQUIREMENTS_STAMP="$VENV_DIR/.jobradar-requirements.sha256"

fail() {
  printf 'Erreur JobRadar : %s\n' "$1" >&2
  exit 1
}

find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && \
      "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

requirements_hash() {
  "$1" -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$REQUIREMENTS_FILE"
}

environment_is_healthy() {
  "$VENV_PYTHON" -m pip check >/dev/null 2>&1 &&
    "$VENV_PYTHON" -c 'import bs4, fastapi, httpx, jobradar, jobspy, playwright, pydantic, uvicorn' >/dev/null 2>&1
}

ensure_capture_browser() {
  if "$VENV_PYTHON" -c 'from jobradar.capture import browser_status; raise SystemExit(0 if browser_status()["available"] else 1)' >/dev/null 2>&1; then
    return 0
  fi
  printf 'Aucun navigateur compatible avec la capture des offres. Installation de Chromium Playwright...\n'
  if ! "$VENV_PYTHON" -m playwright install chromium; then
    printf 'Avertissement JobRadar : Chromium n’a pas pu être installé ; la capture des offres fonctionnera en mode dégradé.\n' >&2
  fi
}

[[ -f "$REQUIREMENTS_FILE" ]] || fail "requirements.txt est introuvable."
BOOTSTRAP_PYTHON="$(find_python)" || fail "Python 3.10 ou plus recent est requis."

if [[ ! -x "$VENV_PYTHON" ]]; then
  printf 'Creation de l’environnement Python...\n'
  "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR" || \
    fail "Impossible de creer .venv (le module venv est peut-etre absent)."
fi

"$VENV_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || \
  fail "L'environnement .venv utilise une version de Python inferieure a 3.10. Supprimez-le puis relancez ce script."

CURRENT_HASH="$(requirements_hash "$VENV_PYTHON")" || fail "Impossible de calculer l'empreinte des dependances."
INSTALLED_HASH=""
[[ -f "$REQUIREMENTS_STAMP" ]] && IFS= read -r INSTALLED_HASH < "$REQUIREMENTS_STAMP"

if [[ "$CURRENT_HASH" != "$INSTALLED_HASH" ]] || ! environment_is_healthy; then
  printf 'Synchronisation des dependances Python...\n'
  "$VENV_PYTHON" -m pip install --disable-pip-version-check -r "$REQUIREMENTS_FILE" || \
    fail "Installation des dependances impossible. Verifiez votre connexion et les messages ci-dessus."
  environment_is_healthy || \
    fail "L'environnement Python reste incoherent apres installation (dependance absente ou incompatible)."
  printf '%s\n' "$CURRENT_HASH" > "$REQUIREMENTS_STAMP"
fi

ensure_capture_browser

cd -- "$SCRIPT_DIR"
exec "$VENV_PYTHON" -m jobradar.cli serve --open "$@"