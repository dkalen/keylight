#!/usr/bin/env bash
# Can a launchd agent (like keylight-watch) read the camera on this Mac?
# Loads a temporary agent that tries to grab frames, waits, prints the result, removes it.
#   scripts/macos-camera-agent-test.sh
set -u
LABEL=local.keylight.camtest
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
OUT=$HOME/keylight-camtest.txt
VPY=$HOME/.local/share/keylight/venv/bin/python
SCRIPT=$HOME/.local/share/keylight/camtest.py
DOMAIN=gui/$(id -u)

[ -x "$VPY" ] || { echo "run scripts/install-macos.sh --with-auto first"; exit 1; }
cat > "$SCRIPT" <<'PY'
import sys, time, cv2
print("python:", sys.executable, flush=True)
for index in range(3):
    cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        print(f"index={index} not opened", flush=True)
        continue
    frames, t = 0, time.time()
    while time.time() - t < 3:
        ok, _ = cap.read()
        frames += ok
    print(f"index={index} frames_in_3s={frames} fps_prop={cap.get(cv2.CAP_PROP_FPS):.1f}", flush=True)
    cap.release()
PY
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key><array><string>$VPY</string><string>$SCRIPT</string></array>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$OUT</string>
    <key>StandardErrorPath</key><string>$OUT</string>
</dict></plist>
PLIST

rm -f "$OUT"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Agent started. If macOS shows a camera prompt for Python, click Allow."
echo "Waiting 25 s..."
sleep 25
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
rm -f "$PLIST" "$SCRIPT"
echo "=== result"
cat "$OUT" 2>/dev/null || echo "(no output)"
echo "=== camera permission entries (TCC)"
log show --last 2m --predicate 'subsystem == "com.apple.TCC" AND eventMessage CONTAINS[c] "camera"' --style compact 2>/dev/null | grep -iE 'python|camera' | tail -8
