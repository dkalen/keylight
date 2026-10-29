#!/usr/bin/env bash
# Verify keylight on macOS, interactively, and collect a report to paste back.
#   scripts/verify-macos.sh
# Installs with --with-auto (launchd agent included), then walks through the checks.
# Everything is logged to ~/keylight-verify.txt.
set -u
REPO=$(cd "$(dirname "$0")/.." && pwd)
REPORT=$HOME/keylight-verify.txt
exec > >(tee "$REPORT") 2>&1

step() { printf '\n=== %s\n' "$*"; }
pause() { printf '\n>>> %s\n    press Enter when ready... ' "$*"; read -r _ </dev/tty; }
KL=$HOME/.local/bin/keylight
KW=$HOME/.local/bin/keylight-watch
VPY=$HOME/.local/share/keylight/venv/bin/python

step "system"
sw_vers; uname -m; date
git -C "$REPO" log -1 --oneline

step "install (--with-auto)"
"$REPO/scripts/install-macos.sh" --with-auto || { echo "INSTALL FAILED"; exit 1; }

step "unit tests"
(cd "$REPO" && "$VPY" -m unittest discover -s tests -t . 2>&1 | tail -3)

step "light"
"$KL" status; echo "exit=$?"
"$KL" info | head -3

step "cameras CoreMediaIO can see"
"$KW" --list-devices

step "probe, camera idle (quit Photo Booth/Zoom/OBS first)"
pause "Quit any app using the camera"
"$KW" --probe

step "Session from direct camera use (Photo Booth)"
"$KL" off
open -a "Photo Booth"
pause "In Photo Booth, pick the capture card (Camera menu) so you see yourself"
"$KW" --probe
sleep 6
echo "light after 6s (expect on, ~4700 K):"; "$KL" status
osascript -e 'quit app "Photo Booth"'
sleep 9
echo "light after quitting Photo Booth (expect off):"; "$KL" status

step "tier 2: can a second app read the card while Photo Booth has it?"
open -a "Photo Booth"
pause "Photo Booth showing the capture card again. If macOS asks Terminal for camera access, allow it"
"$VPY" - <<'PY'
import time, cv2, numpy as np
results = []
for index in range(4):
    cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        continue
    frames, means = 0, []
    t = time.time()
    while time.time() - t < 3:
        ok, img = cap.read()
        if ok:
            frames += 1
            means.append(float(img.mean()))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)); w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    cap.release()
    results.append((index, w, h, frames, round(np.std(means), 2) if means else None, round(np.mean(means), 1) if means else None))
for r in results:
    print("index=%d %dx%d frames_in_3s=%d brightness_std=%s mean=%s" % r)
print("no camera opened" if not results else "")
PY
pause "Did Photo Booth keep showing live video the whole time? (note yes/no in your reply)"
osascript -e 'quit app "Photo Booth"'

step "OBS (skip with Enter if OBS isn't installed yet)"
if [ -d "/Applications/OBS.app" ]; then
    pause "Open OBS with the capture card as a source, enable Tools > WebSocket Server Settings, and Start Virtual Camera"
    "$KW" --probe
    "$KL" auto
    open -a "Photo Booth"
    pause "In Photo Booth, pick 'OBS Virtual Camera'"
    "$KW" --probe
    sleep 6; echo "light (expect on):"; "$KL" status
    osascript -e 'quit app "Photo Booth"'
    pause "Now start a Recording in OBS"
    "$KW" --probe
    pause "Stop the recording"
else
    echo "OBS not installed: skipped"
fi

step "watcher log (last 5 min)"
log show --last 5m --predicate 'eventMessage BEGINSWITH "keylight-watch:"' --style compact 2>/dev/null | tail -40
[ -s "$HOME/Library/Logs/keylight-watch.log" ] && { echo "--- crash log:"; tail -20 "$HOME/Library/Logs/keylight-watch.log"; }

printf '\nDone. Paste the contents of %s back to Claude.\n' "$REPORT"
