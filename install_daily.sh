#!/usr/bin/env bash
# Linux : installe un timer systemd utilisateur qui actualise chaque jour les recherches « quotidiennes ».
# Si le PC est éteint à l'heure prévue, l'exécution est rattrapée au démarrage (Persistent=true).
# Usage : ./install_daily.sh [HEURE]   (par défaut 08:00)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
HOUR="${1:-08:00}"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
# PATH du service : dossiers de claude / codex (et node, utilisé par certaines installations)
EXTRA_PATH=""
for bin in claude codex node; do
  if command -v "$bin" >/dev/null; then EXTRA_PATH="$EXTRA_PATH$(dirname "$(command -v "$bin")"):"; fi
done

cat > "$UNIT_DIR/jobradar.service" <<EOF
[Unit]
Description=JobRadar - actualisation des recherches d'offres

[Service]
Type=oneshot
WorkingDirectory=$DIR
Environment=PATH=${EXTRA_PATH}/usr/local/bin:/usr/bin:/bin
ExecStart=$DIR/.venv/bin/python -m jobradar.cli refresh
EOF

cat > "$UNIT_DIR/jobradar.timer" <<EOF
[Unit]
Description=JobRadar - actualisation quotidienne

[Timer]
OnCalendar=*-*-* $HOUR:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now jobradar.timer
systemctl --user list-timers jobradar.timer
echo "Journal : journalctl --user -u jobradar.service"
