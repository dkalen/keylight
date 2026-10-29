# Phase 2: auto-brightness

**Status:**
- **Built:** tier 1 (the OBS Hub) and tier 3 (the Sun Schedule).
- **Not built:** tier 2 (a shared stream). On Linux it was tested and rejected for now; on macOS it's waiting on the verify script. Details below.

Terms are defined in [GLOSSARY.md](../GLOSSARY.md), and the decisions are recorded in [ADR 0001](adr/0001-obs-hub-with-tiered-frame-sources.md) and [ADR 0002](adr/0002-light-temperature-pinned-to-camera-white-balance.md).

## The room

- The window is about 10 ft to your right, a few feet behind you, and roughly parallel to the camera's line of sight. Daylight mostly lights the room behind you, which is in frame. In the late afternoon it also reaches your right cheek.
- The Light is on your left, opposite the window.
- The camera's exposure and white balance are fully manual, with white balance at 4700 K. The Light is pinned to 4700 K too.

Three consequences follow:
1. More daylight means a brighter background, so the Light needs more brightness by day. The Sun Schedule runs from `night_brightness` (20) up to `day_brightness` (45).
2. Daylight on the far cheek would make a whole-face average read high, and the loop would wrongly dim the key. So only the **Key Side** (your left half) is measured.
3. The background matters. Ratio mode keeps the face at most `max_stops_under` (1) stop below the background.

## Measurement

Implemented in `keylight/vision.py`.

1. **Frame:** `GetSourceScreenshot` of the camera input (auto-detected, or `[obs] source`), as a JPEG 480 px wide.
2. **Face:** YuNet (`cv2.FaceDetectorYN`, 232 KB ONNX model, about 25 ms per frame on CPU). The largest face scoring at least `min_face_score` is used.
3. **Key Side:** the inner 60% of the face box, then the half nearest the Light. When the frame isn't mirrored, the subject's left half appears on the right of the image.
4. **Units:** BT.709 luma in **linear light** (sRGB decoded through a lookup table), so ratios are in stops.
5. **Background:** the mean over `background_roi` if set. Otherwise, the 90th percentile of everything outside a box covering face, hair and shoulders.

## Control

Implemented in `keylight/auto.py`; the math is standard library only. Every `interval` (6 s):

```
target   = linear(target_luma), raised to background / 2^max_stops_under in ratio mode
error    = log2(target / median(last 3 Key Side samples))           # stops
if |error| < deadband (0.15): hold
step     = clamp(gain (0.5) * error, ±max_step (0.33 stop))
new      = clamp(round(current * 2^step), min_brightness, max_brightness)   # at least ±1 when moving
fade over fade_ms (3000)
```

- **Open-loop:** the same stepping toward the Sun Schedule value, never overshooting it.
- **Manual changes:** after the first cycle, the controller remembers the brightness it set. If the light's actual state differs, or the light is off, it pauses for `manual_pause_min` (10 minutes). A light you turn off stays off.
- **No face:** hold for 3 measurements, then fall back to open-loop.
- **Frame source lost:** log it, fall back to open-loop, and try to reconnect every 30 s.
- **Ownership:** the loop runs for the whole Session, whether or not the light is Owned.

## Tiers

| Tier | When | Status |
|---|---|---|
| 1, Hub | OBS is running with the websocket enabled and a capture input | built |
| 2, shared stream | an app other than OBS is reading the card | **not built**, see below |
| 3, open-loop | no frame source | built |

### Tier 2

**Linux: tested and rejected for now** (2026-10-29).

- Two PipeWire consumers can share the card: both got live frames.
- The card only offers `1920x1080 @ 7013/117` (59.94 fps), in NV12 or YUY2, and through PipeWire it only negotiates that exact fraction. Asking for 59/1, 60/1 or 30/1 fails, and so does YUY2 at the default settings.
- Firefox, your main browser, asks for `59/1`. Its PipeWire stream therefore never links, and you get "Success!" with a blank picture. The bug report text is in [firefox-pipewire-framerate-bug.md](firefox-pipewire-framerate-bug.md).
- The workaround is `media.webrtc.camera.allow-pipewire = false`. Firefox then opens the card through V4L2, which works but is exclusive, so there's no tier 2.
- Chrome (flatpak) doesn't use the PipeWire camera either, unless you enable its flag.

**Rejected alternatives:**
- Switching the a6000 to PAL 50p: risks banding under 60 Hz lighting, and probably means formatting the memory card.
- A GStreamer relay that re-times the stream for Firefox: effectively a second Hub, which ADR 0001 decided against.

**Revisit when** Firefox fixes the framerate bug. A tier 2 implementation would also need Session detection based on the PipeWire graph rather than `/proc`, because our own PipeWire consumer would keep `pipewire` holding the card and the Session would never end.

**macOS:** `scripts/verify-macos.sh` checks whether OpenCV can read the card while Photo Booth has it, and whether Photo Booth stays live while it does.

## Tuning

- `keylight auto --save-frame DIR` saves an annotated frame (face, Key Side, background region, 10% grid) and prints the measured values and what the loop would do.
- `keylight-watch -v --dry-run` logs every cycle and sends nothing.
- Start from `target_luma`: adjust it until `keylight auto` holds steady when the picture looks right to you.
