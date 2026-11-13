#!/bin/bash
# Install Quota Deck as a resident systemd service on the HDMI panel.
#   sudo bash install-service.sh            install, enable at boot, start now
#   sudo bash install-service.sh uninstall  stop, disable and remove
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[ "$(id -u)" = 0 ] || { echo 'Run with sudo: sudo bash install-service.sh'; exit 1; }
USER_NAME=${SUDO_USER:-}
[ -n "$USER_NAME" ] && [ "$USER_NAME" != root ] || { echo 'Run via sudo from your normal account.'; exit 1; }
USER_ID=$(id -u "$USER_NAME")
UNIT=/etc/systemd/system/quota-deck.service

stop_manual() {
    # A panel started by run-quota.sh would hold the HDMI output; close it cleanly (restores the VT).
    if pgrep -f "$PWD/standalone.py --quota" >/dev/null; then
        echo 'Closing the manually started panel...'
        pkill -TERM -f "$PWD/standalone.py --quota" || true
        for _ in $(seq 20); do pgrep -f "$PWD/standalone.py --quota" >/dev/null || break; sleep 0.5; done
    fi
}

if [ "${1:-}" = uninstall ]; then
    systemctl disable --now quota-deck.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo 'Quota Deck service removed.'
    exit 0
fi

cat > "$UNIT" <<EOF
[Unit]
Description=Quota Deck HDMI panel
After=display-manager.service network-online.target
Wants=network-online.target

[Service]
Type=simple
# standalone.py starts Xorg on a free VT as root and runs the panel itself as $USER_NAME.
Environment=SUDO_UID=$USER_ID PYTHONUNBUFFERED=1 LANG=en_US.UTF-8
ExecStartPre=/bin/sleep 3
ExecStart=/usr/bin/python3 $PWD/standalone.py --quota
KillSignal=SIGTERM
TimeoutStopSec=20
# The on-screen power button exits cleanly and stays closed; crashes restart.
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical.target
EOF

stop_manual
systemctl daemon-reload
systemctl enable quota-deck.service
systemctl restart quota-deck.service
sleep 4
systemctl --no-pager --lines=5 status quota-deck.service || true
echo
echo 'Installed. It starts at boot and restarts after crashes.'
echo 'Reopen after using the power button: sudo systemctl start quota-deck'
echo 'Logs: journalctl -u quota-deck -f      Remove: sudo bash install-service.sh uninstall'
