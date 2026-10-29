#!/usr/bin/env bash
# Undo a `sudo` install: give keylight's per-user files back to you.
#   scripts/fix-ownership.sh        (run as yourself; it asks for sudo itself)
set -euo pipefail

owner=${SUDO_USER:-$(id -un)}
[ "$owner" = root ] && { echo "error: run this as your own user, not from a root shell" >&2; exit 1; }
home=$(eval echo "~$owner")
config=${XDG_CONFIG_HOME:-$home/.config}

paths=(
    "$home/.local/share/keylight"
    "$home/.local/bin/keylight"
    "$home/.local/bin/keylight-watch"
    "$config/keylight"
    "$home/Library/LaunchAgents/local.keylight.watch.plist"
    "$home/Library/Logs/keylight-watch.log"
    "$home/Library/Caches/pip"
    "$config/systemd/user/keylight-watch.service"
    "$home/.cache/pip"
)
existing=()
for p in "${paths[@]}"; do
    [ -e "$p" ] || [ -L "$p" ] && existing+=("$p")
done
[ ${#existing[@]} -eq 0 ] && { echo "nothing to fix"; exit 0; }

group=$(id -gn "$owner")
sudo chown -R -h "$owner:$group" "${existing[@]}"
printf 'now owned by %s:\n' "$owner"; printf '  %s\n' "${existing[@]}"
echo "next: scripts/install-macos.sh (or install-linux.sh), without sudo"
