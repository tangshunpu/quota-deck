#!/bin/bash
# Run as the normal panel user, without sudo.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[ "$(id -u)" != 0 ] || { echo 'Run this script as the normal panel user, without sudo.'; exit 1; }
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/quota-deck-web.service" <<UNIT
[Unit]
Description=Quota Deck LAN controller and data service

[Service]
Type=simple
WorkingDirectory=$PWD
Environment="PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" PYTHONUNBUFFERED=1 LANG=en_US.UTF-8
ExecStart=/usr/bin/python3 $PWD/server.py --host 0.0.0.0 --port 18768
Restart=on-failure
RestartSec=3
TimeoutStopSec=10

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable quota-deck-web.service
systemctl --user restart quota-deck-web.service
systemctl --user is-active quota-deck-web.service
