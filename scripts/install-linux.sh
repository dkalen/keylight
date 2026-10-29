#!/usr/bin/env bash
# Install keylight + the keylight-watch systemd user service. Safe to re-run.
# --with-auto adds a venv with numpy/opencv and the face model (closed-loop auto-brightness).
source "$(dirname "$0")/_common.sh" "$@"

PY=$(find_python)
install_package "$PY"

UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/keylight-watch.service" <<UNIT
[Unit]
Description=Keep the Elgato Key Light on during camera Sessions
Documentation=file://$SRC/README.md

[Service]
ExecStart=$BIN/keylight-watch
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable keylight-watch.service >/dev/null
systemctl --user restart keylight-watch.service
echo "keylight-watch.service enabled and (re)started; logs: journalctl --user -u keylight-watch -f"
