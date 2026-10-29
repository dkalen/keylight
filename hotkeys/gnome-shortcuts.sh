#!/usr/bin/env bash
# Add (or update) GNOME custom keyboard shortcuts for keylight. Safe to re-run.
#   ./gnome-shortcuts.sh           install the shortcuts below
#   ./gnome-shortcuts.sh --remove  remove them
# Edit the SHORTCUTS table to taste. Bindings use GNOME accelerator syntax; the
# defaults were checked against GNOME 50 bindings (Super+Alt+arrows are taken).
set -euo pipefail

KEYLIGHT=${KEYLIGHT_BIN:-$HOME/.local/bin/keylight}   # absolute: GNOME's PATH may lack ~/.local/bin
SHORTCUTS=(
    # id            | binding              | args
    "toggle         |<Super><Alt>l         |toggle"
    "brighter       |<Super><Alt>equal     |brightness +10"
    "dimmer         |<Super><Alt>minus     |brightness -10"
)

SCHEMA=org.gnome.settings-daemon.plugins.media-keys
BASE=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings

current=$(gsettings get $SCHEMA custom-keybindings)
ours=()
for row in "${SHORTCUTS[@]}"; do
    ours+=("$BASE/keylight-$(echo "${row%%|*}" | xargs)/")
done

# Rebuild the list: keep everyone else's entries, then add ours unless removing.
new=$(python3 - "$current" "${1:-}" "${ours[@]}" <<'PY'
import ast, sys
current, mode, ours = sys.argv[1], sys.argv[2], sys.argv[3:]
items = ast.literal_eval(current.removeprefix("@as ").strip())
items = [p for p in items if "/keylight-" not in p]   # also drops shortcuts removed from the table
if mode != "--remove":
    items += ours
print(str(items))
PY
)

if [ "${1:-}" = "--remove" ]; then
    gsettings set $SCHEMA custom-keybindings "$new"
    for path in "${ours[@]}"; do
        gsettings reset-recursively "$SCHEMA.custom-keybinding:$path" 2>/dev/null || true
    done
    echo "removed keylight shortcuts"
    exit 0
fi

for row in "${SHORTCUTS[@]}"; do
    IFS='|' read -r id binding args <<<"$row"
    id=$(echo "$id" | xargs); binding=$(echo "$binding" | xargs)
    path="$BASE/keylight-$id/"
    gsettings set "$SCHEMA.custom-keybinding:$path" name "Key Light: $id"
    gsettings set "$SCHEMA.custom-keybinding:$path" command "$KEYLIGHT -q $args"
    gsettings set "$SCHEMA.custom-keybinding:$path" binding "$binding"
    echo "  $binding  ->  keylight $args"
done
gsettings set $SCHEMA custom-keybindings "$new"
echo "installed; see Settings > Keyboard > Keyboard Shortcuts > Custom Shortcuts"
