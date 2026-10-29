#!/usr/bin/env bash
# Verify keylight on macOS, interactively, and collect a report to paste back.
#   scripts/verify-macos.sh                 all sections
#   scripts/verify-macos.sh tier2 obs log   just those (install session tier2 obs log)
# Logged to ~/keylight-verify.txt.
set -u
REPO=$(cd "$(dirname "$0")/.." && pwd)
REPORT=$HOME/keylight-verify.txt
exec > >(tee "$REPORT") 2>&1

step() { printf '\n=== %s\n' "$*"; }
pause() { printf '\n>>> %s\n    press Enter when ready... ' "$*"; read -r _ </dev/tty; }
ask() { printf '\n??? %s ' "$*"; read -r answer </dev/tty; echo "answer: $answer"; }
KL=$HOME/.local/bin/keylight
KW=$HOME/.local/bin/keylight-watch
VPY=$HOME/.local/share/keylight/venv/bin/python
cd "$HOME"    # never run from inside the checkout

section_install() {
    step "system"
    sw_vers; uname -m; date
    git -C "$REPO" log -1 --oneline
    step "install (--with-auto)"
    "$REPO/scripts/install-macos.sh" --with-auto || { echo "INSTALL FAILED"; exit 1; }
    step "unit tests"
    (cd "$REPO" && "$VPY" -m unittest discover -s tests -t . 2>&1 | tail -3)
    step "light + config"
    "$KL" status; echo "exit=$?"
    "$KL" auto | head -1
    step "cameras CoreMediaIO can see"
    "$KW" --list-devices
}

section_session() {
    step "probe, camera idle"
    pause "Quit any app using the camera (Photo Booth, Zoom, OBS)"
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
}

section_tier2() {
    step "tier 2: can a second app read the card while Photo Booth has it?"
    echo "Terminal needs camera access: System Settings > Privacy & Security > Camera > enable your terminal app."
    echo "If it isn't listed, this test triggers the prompt; allow it, then re-run: scripts/verify-macos.sh tier2"
    open -a "Photo Booth"
    pause "Photo Booth showing the capture card"
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
if not results:
    print("no camera opened")
PY
    ask "Did Photo Booth keep showing live video the whole time? [y/n]"
    osascript -e 'quit app "Photo Booth"'
}

section_obs() {
    step "OBS"
    [ -d "/Applications/OBS.app" ] || { echo "OBS not in /Applications: skipped"; return; }
    cat <<'TXT'
The virtual camera is a macOS camera extension and needs one-time approval:
  1. OBS must be in /Applications (not run from Downloads or a disk image).
  2. Click Start Virtual Camera once; macOS blocks it and asks for approval.
  3. System Settings > General > Login Items & Extensions > Camera Extensions:
     turn on OBS Virtual Camera (older macOS: Privacy & Security > Allow).
  4. Quit and reopen OBS, then Start Virtual Camera again.
TXT
    pause "OBS open with the capture card as a source, WebSocket server enabled, Virtual Camera started"
    echo "--- devices (expect OBS Virtual Camera listed):"; "$KW" --list-devices
    "$KW" --probe
    "$KL" auto
    "$KL" off
    open -a "Photo Booth"
    pause "In Photo Booth, pick 'OBS Virtual Camera'"
    "$KW" --probe
    sleep 6; echo "light (expect on):"; "$KL" status
    osascript -e 'quit app "Photo Booth"'
    sleep 9; echo "light after quitting Photo Booth (expect off):"; "$KL" status
    pause "Now start a Recording in OBS"
    "$KW" --probe
    pause "Stop the recording"
}

section_log() {
    step "watcher log (last 15 min)"
    log show --last 15m --info --predicate 'eventMessage CONTAINS "keylight-watch:"' --style compact 2>/dev/null | tail -40
    [ -s "$HOME/Library/Logs/keylight-watch.log" ] && { echo "--- crash log:"; tail -20 "$HOME/Library/Logs/keylight-watch.log"; }
}

sections=("$@")
[ ${#sections[@]} -eq 0 ] && sections=(install session tier2 obs log)
for s in "${sections[@]}"; do
    case $s in
        install|session|tier2|obs|log) "section_$s" ;;
        *) echo "unknown section: $s (install session tier2 obs log)"; exit 2 ;;
    esac
done
printf '\nDone. Paste the contents of %s back to Claude.\n' "$REPORT"
