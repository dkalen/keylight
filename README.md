# keylight

Keeps an Elgato Key Light (tested with a Key Light MK.2, model 20GAK9902, firmware 1.0.4) on for exactly as long as you're on camera, and sets its brightness from the live picture. It runs on macOS and Fedora, and uses only the light's local HTTP API. Nothing here talks to Elgato's app or Control Center.

- **`keylight`**: a CLI for on/off/brightness/temperature. Exit codes are meant for scripts and hotkeys.
- **`keylight-watch`**: a background service that follows **Sessions**. It turns the light on when a Session starts, keeps it at the right brightness, and turns it off afterwards.
- **Hotkeys** for GNOME and Hammerspoon.

The terms used here (Session, Hub, Owned, Key Side, Sun Schedule and others) are defined in [GLOSSARY.md](GLOSSARY.md). The "why" behind the main decisions is in [docs/adr/](docs/adr/).

## How it works

A **Session** is any of these:
- A non-OBS app reading the capture card directly. This means calls still work when OBS is closed or broken.
- An app reading the **OBS Virtual Camera**. This is the normal setup, with OBS as the Hub.
- **OBS recording or streaming** with the camera set up as one of its sources, whether or not it's in the current scene. A screen-only recording doesn't count.

When a Session starts, the watcher turns the light on with these settings:
- **Colour temperature:** the pinned temperature, 4700 K, which matches the camera's white balance ([ADR 0002](docs/adr/0002-light-temperature-pinned-to-camera-white-balance.md)).
- **Brightness:** the **Sun Schedule** value for the current time at your location.

The watcher only turns the light off afterwards if it turned it on itself (**Owned**). If the light was already on when the Session started, the watcher only sets it to the pinned colour temperature. Manual changes during a Session are never fought.

While the Session lasts, auto-brightness adjusts the light using the best frame source available ([ADR 0001](docs/adr/0001-obs-hub-with-tiered-frame-sources.md)):

| Tier | Frame source | Control |
|---|---|---|
| 1 | OBS screenshots of the camera source, over obs-websocket | closed-loop: Key Side of your face toward a target, and within 1 stop of the background |
| 2 | a shared stream next to the call app | not built. Linux: blocked by a Firefox PipeWire bug; macOS: works, but skipped on the managed work Mac (see [Phase 2](docs/phase2-auto-brightness.md#tier-2)) |
| 3 | none | open-loop: follow the Sun Schedule |

How closed-loop control behaves:
- **Pacing:** it works in stops, in linear light. It measures every 6 s, ignores errors under 0.15 stop, moves at most ⅓ stop per update, and fades each change over 3 s, so the light never visibly pulses.
- **Manual changes:** any change you make (CLI, hotkey, button on the light) pauses it for 10 minutes.
- **No face in frame:** it holds the current brightness for 3 measurements, then falls back to open-loop.

## Install

```sh
git clone git@github.com:<you>/keylight.git ~/keylight && cd ~/keylight

scripts/install-linux.sh --with-auto     # Fedora: ~/.local/bin/keylight{,-watch} + systemd user service
scripts/install-macos.sh --with-auto     # macOS (needs Python 3.11+: `brew install python`) + launchd agent
scripts/verify-macos.sh                  # macOS: guided checks; writes ~/keylight-verify.txt
scripts/uninstall-{linux,macos}.sh
```

**`--with-auto`** creates a venv at `~/.local/share/keylight/venv` with numpy and opencv-python-headless. It also downloads the YuNet face model (232 KB, SHA-256 pinned). Without it, everything works except tier 1, so brightness follows the Sun Schedule. Once a machine has the venv, re-running the installer keeps it.

The installers are idempotent: re-run one after `git pull` to update the code and restart the service. **The config lives in the repo** (`config.toml`), and the installer symlinks `~/.config/keylight/config.toml` to it, so both machines share one config through git. An existing non-symlink config is moved aside as `config.toml.bak-<date>`. Don't run the installers with `sudo`; they refuse to. If an earlier `sudo` run left root-owned files behind, `scripts/fix-ownership.sh` gives them back.

### One-time setup

- **Light IP:** give the light a static DHCP mapping (in OPNsense: Services → DHCPv4 → [LAN] → Static mappings). `keylight info` shows its MAC. Put the IP in `[light] host`. If the light is ever moved to an IoT VLAN, allow TCP 9123 from the trusted network. mDNS fallback doesn't cross subnets.
- **Location:** create `~/.config/keylight/local.toml` with `[location]`, `latitude = …` and `longitude = …`, one per machine. It isn't in git and is merged over the shared config. Alternatively, set `KEYLIGHT_LATITUDE` / `KEYLIGHT_LONGITUDE`. City-level precision (one decimal) is plenty for the Sun Schedule. Without a location, sessions start at `day_brightness`.
- **Camera:** set the a6000's white balance to Color Temp **4700 K**, so it matches `[light] kelvin`.
- **OBS:**
  - Under Tools → WebSocket Server Settings, enable the server. The watcher reads the password from OBS's own config on each machine, so it never goes in the repo.
  - On Linux, the virtual camera needs v4l2loopback: `sudo dnf install akmod-v4l2loopback` from RPM Fusion, then reboot.
  - In call apps, pick **OBS Virtual Camera**.
  - Start OBS with `--startvirtualcam`, because OBS doesn't restart the virtual camera on its own. On GNOME, copy the launcher and edit its `Exec` line: `sed 's/^Exec=obs$/Exec=obs --startvirtualcam/' /usr/share/applications/com.obsproject.Studio.desktop > ~/.local/share/applications/com.obsproject.Studio.desktop`.
  - Without a running virtual camera, v4l2loopback (with `exclusive_caps`) hides the device. Call apps then offer only the card, which OBS is already streaming from, so the call gets no video while the light still comes on. Firefox can also keep the card open after such a failure, which keeps the light on until Firefox restarts.
- **Calibration:** run `keylight auto --save-frame ~/Pictures`. The saved frame shows your face box, the Key Side box and a 10% grid. Read the background rectangle off the grid and set `[auto] background_roi = [x, y, w, h]`. Then adjust `target_luma` until `keylight auto` says "would set: N% -> N%" when you look right on camera.

## CLI

```
keylight status [--json]            on  brightness 35%  4695 K (213 mired)
keylight on | off | toggle
keylight brightness 40 | +10 | -10  absolute 0-100 (0 = off) or relative, clamped to [min_brightness, 100]
keylight temp 4700                  Kelvin, clamped to 2900-7000
keylight set -b 30 -k 4700 [--on]
keylight preset [NAME]              apply/list your own presets (none by default)
keylight auto [--save-frame DIR]    one auto-brightness measurement; shows what it would do, sends nothing
keylight info [--json]              model, firmware, serial, Wi-Fi
keylight discover [--json]          browse mDNS for _elg._tcp
```

Global flags: `--host IP`, `--config PATH`, and `-q` (no output on success). `brightness`, `temp` and `set` accept `--fade-ms N`, which steps toward the target with up to 40 PUTs. A fade only happens while the light is on. Switching on and off already fades on the device itself.

| Exit code | Meaning |
|---|---|
| 0 | success |
| 1 | other error |
| 2 | usage or config error |
| 3 | light (or OBS) unreachable |
| 4 | unsupported device or unexpected response (HTTP error, odd JSON, `tls=` device, OBS refusal) |

## Watcher

```sh
keylight-watch --list-devices   # cameras the probe can see
keylight-watch --probe          # in-use/idle/absent now, who holds the device, OBS connection
keylight-watch -v --dry-run     # foreground; logs every decision, including each auto cycle, and sends nothing
keylight-watch --no-auto        # Sessions only, no auto-brightness
```

Session detection is pushed, not polled, except for the macOS microphone check below. Each change wakes the watcher, which then re-reads the real state, so a missed event only delays things until the next one. Between events it sleeps; when idle it uses no CPU at all.
- **Linux:** inotify reports every open or close of a `/dev/video*` node, and video nodes or their `by-id` links appearing or disappearing (KVM switch, renumbering). inotify can't say who opened a node, so each event triggers a scan of `/proc/*/fd` for your processes holding the card or the virtual camera. Processes in `ignore_processes` don't count, which is how OBS's own hold on the card is excluded. A scan costs about 10 ms.
- **macOS:** CoreMediaIO listeners report a camera starting or stopping, or a camera arriving or leaving, and the watcher reads `DeviceIsRunningSomewhere` through `ctypes`. CoreMediaIO delivers those notifications, and refreshes its device list, only through the main thread's run loop, so the watcher runs one there. CoreMediaIO can't say *which* app runs a device, so the watcher asks OBS whether its own camera source is live (`GetSourceActive`).
  - **OBS camera source live:** use of the card is attributed to OBS, unless a call app is capturing a microphone. CoreAudio, unlike CoreMediaIO, does say which app is recording, so a Zoom or Meet call joined next to OBS still counts, and closed-loop auto-brightness runs for it from OBS's screenshots. Calls keep the mic open while muted. CoreAudio can't push this change (a listener on a process's input flag registers but never fires, and the mic's own running flag stays set while OBS has it open), so in this one state, card in use and OBS's camera live, the watcher re-checks the mic every 0.3 s, reading only the call apps' processes (about 4 ms each, roughly 1.5% of one core while OBS holds the card); the light comes on about 1–1.5 s after the call app opens the mic. Only apps in `mic_call_apps` count: Zoom, Teams, the major browsers (by their helper processes), Slack, FaceTime and Webex. So OBS's own mic, dictation or a meeting recorder don't. A browser using the mic still counts, as does a call with your camera off. `keylight-watch --probe` lists apps that are capturing but not listed; `mic_sessions = false` turns this off. Recording or streaming still counts as a Session.
  - **OBS closed, or open without a live camera source** (for example, a screen recording during a call): use of the card counts.
  - **OBS running with its websocket off:** the watcher can't tell, so it assumes OBS holds the camera.
- **OBS:** a second, events-only obs-websocket connection reports recordings, streams and the virtual camera starting or stopping, camera inputs being added or removed, the camera source becoming live or hidden, and OBS quitting. While OBS isn't running there's nothing to listen to, so the watcher tries to connect when a camera changes (OBS opening the card is how a starting OBS shows up), retrying after 1, 2, 4, 8 and 16 s, then waiting for the next camera change.
- **Fallback:** if the camera can't push changes (inotify or CoreMediaIO unavailable), the watcher logs it and polls everything every `poll_interval`.

When the camera disappears (a KVM switch), the light is left as is. If the camera stays gone longer than `absent_grace` (15 s), the Session is forgotten. One known gap: if a call moves to the other machine, neither machine Owns the light, so it stays on until you turn it off.

## Config reference

Every key, with its default and a comment, is in [config.example.toml](config.example.toml). The live config is [config.toml](config.toml). You can override the path with `$KEYLIGHT_CONFIG` or `--config`. Unknown keys are rejected.

On top of that, per-machine values come from `local.toml` beside the config (`~/.config/keylight/local.toml`, or `$KEYLIGHT_LOCAL_CONFIG`), which is merged over the shared file. After that come `KEYLIGHT_LATITUDE` / `KEYLIGHT_LONGITUDE`. `local.toml` is gitignored.

| Section | Holds |
|---|---|
| `[light]` | host, port, timeout, retries, mdns_fallback, min_brightness, **kelvin** (pinned) |
| `[location]` | latitude and longitude for the Sun Schedule (keep in `local.toml`) |
| `[presets.NAME]` | optional brightness/kelvin levels for `keylight preset` |
| `[watch]` | physical and virtual devices per OS, obs_sessions, on_action (`auto` / `restore` / `preset:NAME`), delays, ignore_processes |
| `[obs]` | websocket host and port; password, password_file or auto; capture source (auto: the first video capture input) |
| `[auto]` | interval, target_luma, deadband/gain/max step, brightness clamps, fade, manual pause, ratio mode, background_roi, key_side, mirrored, Sun Schedule day/night levels |

Device limits measured on firmware 1.0.4: brightness 0–100, temperature 143–344 mired. Values outside those ranges get an HTTP 400.

## Hotkeys

- **GNOME:** `hotkeys/gnome-shortcuts.sh` installs Super+Alt+L to toggle and Super+Alt+= / - for brightness ±10. `--remove` undoes it. These keys avoid GNOME 50's own bindings. To add a shortcut by hand, go to Settings → Keyboard → Custom Shortcuts and use the absolute path, such as `/home/<you>/.local/bin/keylight -q toggle`.
- **macOS:** copy `hotkeys/hammerspoon.lua` into `~/.hammerspoon/init.lua`. It binds Ctrl+Alt+Cmd+L/↑/↓. Shortcuts.app also works: a "Run Shell Script" action with `~/.local/bin/keylight -q toggle`, plus a keyboard shortcut.

Any brightness hotkey pauses auto-brightness for 10 minutes, which is how you take over.

## Logs

- **Linux:** `journalctl --user -u keylight-watch -f`
- **macOS:** `log stream --predicate 'eventMessage BEGINSWITH "keylight-watch:"'`. Crash tracebacks go to `~/Library/Logs/keylight-watch.log`.

At the default level you see startup, Session starts and ends, light actions, frame-source tier changes, manual-change pauses, and connectivity changes. `-v` adds every auto cycle (`auto: closed-loop face=0.141 bg=0.402 err=+0.06st 35% -> 35%`).

## Troubleshooting

- **`unreachable` (exit 3):**
  - Run `curl http://<ip>:9123/elgato/lights` from the same machine.
  - If that fails too, check the DHCP mapping, any VLAN rule for TCP 9123, and the light's Wi-Fi (`keylight info` shows the RSSI).
  - The light can be slow to answer after a long idle period. The watcher retries.
- **`HTTP 403`:** patched firmware (CVE-2025-7202) rejects requests that carry an `Origin` header. This tool never sends one, so a proxy in the path must be adding it.
- **`advertises tls=`:** the light uses Elgato's newer mTLS protocol. That's not supported.
- **Light never comes on:** run `keylight-watch --probe` during the call. If it says `idle`, the app is probably reading a device that isn't in `[watch]`. If it says `in-use` with no call running, some other process holds the device, and `--probe` names it.
- **Auto-brightness stuck in open-loop:** run `keylight auto` and read the reason it prints. The usual ones are:
  - The OBS websocket is disabled.
  - There's no venv, so you need to re-run the installer with `--with-auto`.
  - The source name is wrong, so set `[obs] source`.
- **Firefox says "Success!" but the video is blank:** Firefox's PipeWire camera path can't negotiate this card's 59.94 fps ([details](docs/firefox-pipewire-framerate-bug.md)). In `about:config`, set `media.webrtc.camera.allow-pipewire` to `false` and restart Firefox.
- **Light came on but didn't turn off:** that's by design if the light was already on when the Session started, or if the camera was gone longer than `absent_grace`.

## Development

```sh
python3 -m unittest discover -s tests -t .          # standard-library parts; vision tests skip
python3 -m venv .venv && .venv/bin/pip install numpy opencv-python-headless
.venv/bin/python -m unittest discover -s tests -t .  # everything
```

None of the tests need the real light or OBS. They use:
- a mock HTTP light (`tests/mock_light.py`);
- a fake obs-websocket server with real RFC 6455 framing (`tests/test_obs.py`);
- a fake `/dev`, `/proc` and `/sys` tree for the Linux probe.

`models/` and `.venv/` are gitignored.
