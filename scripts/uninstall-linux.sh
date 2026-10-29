#!/usr/bin/env bash
# Remove the keylight-watch user service and installed files. Safe to re-run.
source "$(dirname "$0")/_common.sh" "$@"

UNIT=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/keylight-watch.service
systemctl --user disable --now keylight-watch.service 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload
echo "removed keylight-watch.service"
remove_package
