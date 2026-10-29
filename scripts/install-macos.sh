#!/usr/bin/env bash
# Install keylight + the keylight-watch launchd user agent. Safe to re-run.
# --with-auto adds a venv with numpy/opencv and the face model (closed-loop auto-brightness).
source "$(dirname "$0")/_common.sh" "$@"

LABEL=local.keylight.watch
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
DOMAIN=gui/$(id -u)

PY=$(find_python)
install_package "$PY"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$BIN/keylight-watch</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>ProcessType</key><string>Background</string>
    <!-- Normal logging goes to the unified log; this only catches crashes. -->
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/keylight-watch.log</string>
</dict>
</plist>
PLIST

# bootout is asynchronous: wait until the old agent is really gone, or
# bootstrap fails with "5: Input/output error".
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
for _ in $(seq 1 20); do
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
    sleep 0.5
done
if ! launchctl bootstrap "$DOMAIN" "$PLIST"; then
    sleep 2
    launchctl bootstrap "$DOMAIN" "$PLIST" || { echo "error: launchctl bootstrap failed (see above)" >&2; exit 1; }
fi
echo "$LABEL loaded; logs: log stream --predicate 'eventMessage BEGINSWITH \"keylight-watch:\"'"
