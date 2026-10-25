#!/usr/bin/env bash
# Installe ou met a jour le timer systemd utilisateur de JobRadar (Linux).
# Usage : ./install_daily.sh [HH:MM] (08:00 par defaut)
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
readonly SCHEDULE_TIME="${1:-08:00}"
readonly UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

fail() {
  printf 'Erreur JobRadar : %s\n' "$1" >&2
  exit 1
}

[[ $# -le 1 ]] || fail "usage : ./install_daily.sh [HH:MM]"
[[ "$SCHEDULE_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || \
  fail "l'heure doit respecter le format HH:MM (exemple : 08:00)."
command -v systemctl >/dev/null 2>&1 || fail "systemctl est introuvable ; ce script requiert systemd sous Linux."
[[ -x "$VENV_PYTHON" ]] || fail "lancez d'abord ./run.sh pour creer l'environnement Python."
"$VENV_PYTHON" -c 'import jobradar' >/dev/null 2>&1 || \
  fail "l'installation JobRadar est incomplete ; relancez ./run.sh."

# Les retours a la ligne rendraient le fichier d'unite ambigu ou invalide.
[[ "$SCRIPT_DIR" != *$'\n'* && "$UNIT_DIR" != *$'\n'* ]] || fail "un chemin contient un retour a la ligne non pris en charge."

# Echappe une valeur placee entre guillemets dans une directive systemd.
systemd_quote_value() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//%/%%}"
  printf '%s' "$value"
}

SYSTEMD_DIR="$(systemd_quote_value "$SCRIPT_DIR")"
mkdir -p -- "$UNIT_DIR"

# Conserve les emplacements des assistants IA lorsqu'ils ne sont pas dans le PATH systeme.
EXTRA_PATH=""
for binary in claude codex node; do
  if binary_path="$(command -v "$binary" 2>/dev/null)"; then
    binary_dir="$(dirname -- "$binary_path")"
    [[ "$binary_dir" != *$'\r'* && "$binary_dir" != *$'\n'* && "$binary_dir" != *:* ]] || \
      fail "le dossier de $binary contient un caractere interdit (deux-points ou retour a la ligne)."
    [[ ":$EXTRA_PATH:" == *":$binary_dir:"* ]] || EXTRA_PATH="${EXTRA_PATH:+$EXTRA_PATH:}$binary_dir"
  fi
done
SERVICE_PATH="${EXTRA_PATH:+$EXTRA_PATH:}/usr/local/bin:/usr/bin:/bin"
SYSTEMD_PATH="$(systemd_quote_value "$SERVICE_PATH")"

STAGING_DIR="$(mktemp -d --tmpdir="$UNIT_DIR" .jobradar-units.XXXXXX)"
SERVICE_TMP="$STAGING_DIR/jobradar.service"
TIMER_TMP="$STAGING_DIR/jobradar.timer"
SERVICE_BACKUP="$STAGING_DIR/jobradar.service.previous"
TIMER_BACKUP="$STAGING_DIR/jobradar.timer.previous"
SERVICE_EXISTED=false
TIMER_EXISTED=false
cleanup() {
  rm -f -- "$SERVICE_TMP" "$TIMER_TMP" "$SERVICE_BACKUP" "$TIMER_BACKUP"
  rmdir -- "$STAGING_DIR" 2>/dev/null || true
}
trap cleanup EXIT

cat > "$SERVICE_TMP" <<EOF
[Unit]
Description=JobRadar - actualisation des recherches d'offres

[Service]
Type=oneshot
WorkingDirectory="$SYSTEMD_DIR"
Environment="PATH=$SYSTEMD_PATH"
ExecStart="$SYSTEMD_DIR/.venv/bin/python" -m jobradar.cli refresh
EOF

cat > "$TIMER_TMP" <<EOF
[Unit]
Description=JobRadar - actualisation quotidienne

[Timer]
OnCalendar=*-*-* $SCHEDULE_TIME:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Valide la syntaxe avant de toucher aux unites actives. Certaines distributions
# minimales ne fournissent pas systemd-analyze : systemctl reste alors la validation disponible.
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "$SERVICE_TMP" "$TIMER_TMP" || \
    fail "les nouvelles unites systemd sont invalides ; aucune modification n'a ete appliquee."
fi

# Sauvegarde l'etat exact des fichiers et du timer afin de pouvoir restaurer
# l'installation precedente si systemd refuse la nouvelle configuration.
if [[ -e "$UNIT_DIR/jobradar.service" || -L "$UNIT_DIR/jobradar.service" ]]; then
  cp -a -- "$UNIT_DIR/jobradar.service" "$SERVICE_BACKUP"
  SERVICE_EXISTED=true
fi
if [[ -e "$UNIT_DIR/jobradar.timer" || -L "$UNIT_DIR/jobradar.timer" ]]; then
  cp -a -- "$UNIT_DIR/jobradar.timer" "$TIMER_BACKUP"
  TIMER_EXISTED=true
fi
WAS_ENABLED=false
WAS_ACTIVE=false
systemctl --user is-enabled --quiet jobradar.timer 2>/dev/null && WAS_ENABLED=true
systemctl --user is-active --quiet jobradar.timer 2>/dev/null && WAS_ACTIVE=true

rollback_units() {
  if $SERVICE_EXISTED; then
    mv -f -- "$SERVICE_BACKUP" "$UNIT_DIR/jobradar.service"
  else
    rm -f -- "$UNIT_DIR/jobradar.service"
  fi
  if $TIMER_EXISTED; then
    mv -f -- "$TIMER_BACKUP" "$UNIT_DIR/jobradar.timer"
  else
    rm -f -- "$UNIT_DIR/jobradar.timer"
  fi
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  if $WAS_ENABLED; then
    systemctl --user enable jobradar.timer >/dev/null 2>&1 || true
  else
    systemctl --user disable jobradar.timer >/dev/null 2>&1 || true
  fi
  if $WAS_ACTIVE; then
    systemctl --user start jobradar.timer >/dev/null 2>&1 || true
  else
    systemctl --user stop jobradar.timer >/dev/null 2>&1 || true
  fi
}

# Chaque renommage est atomique sur le meme systeme de fichiers. En cas d'echec
# ulterieur, rollback_units remet les deux anciennes unites et leur etat.
mv -f -- "$SERVICE_TMP" "$UNIT_DIR/jobradar.service"
mv -f -- "$TIMER_TMP" "$UNIT_DIR/jobradar.timer"
if ! systemctl --user daemon-reload; then
  rollback_units
  fail "impossible de recharger les unites systemd ; la configuration precedente a ete restauree."
fi
if ! systemctl --user enable --now jobradar.timer; then
  rollback_units
  fail "impossible d'activer le timer ; la configuration precedente a ete restauree."
fi
systemctl --user list-timers jobradar.timer --no-pager
printf 'Planification installee a %s. Journal : journalctl --user -u jobradar.service\n' "$SCHEDULE_TIME"
