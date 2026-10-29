# OBS is the camera hub; auto-brightness degrades by frame source

On Linux, a UVC card can only be streamed by one process at a time (a second `ffmpeg` gets `EBUSY` while OBS streams), so measuring the face during a call needs one owner that shares frames. We made OBS that owner: calls use the OBS Virtual Camera, and auto-brightness pulls downscaled screenshots over obs-websocket. We chose this over an ffmpeg → v4l2loopback tee because OBS is already in use, works on macOS too, and survives KVM re-enumeration without a pipeline of our own to supervise.

Calls must still work without OBS, so the light follows any Session regardless of the hub. Brightness control falls back by available Frame Source: Hub screenshots (closed-loop), then a shared stream next to the call app (PipeWire on Linux, concurrent AVFoundation access on macOS; only where validated), then the Sun Schedule (open-loop).

## Consequences

- Phase 1 needs an obs-websocket client, for recording/streaming Sessions. It's a small stdlib one, so the watcher stays dependency-free.
- On macOS, CoreMediaIO can't say which process is using the card. So while OBS is running, direct use of the card isn't counted as a Session there, and calls on the Mac must go through the virtual camera.
- Firefox on this machine runs with `media.webrtc.camera.allow-pipewire = false`. Its PipeWire camera path can't negotiate the card's only frame rate (59.94 fps as 7013/117), so the picture stays blank. Re-enabling the setting breaks direct calls from Firefox, and that also rules out Linux tier 2 for now (see the Phase 2 doc).
- The work Mac has no Hub. It's MDM-managed and doesn't offer the setting to approve OBS's camera extension, so the OBS Virtual Camera can't run there. We chose not to ask IT for an exception: calls on the Mac use the card directly with OBS closed, and OBS screenshots are still used whenever OBS is open, for example while recording.
- macOS tier 2 works technically: a second AVFoundation reader got 60 fps while Photo Booth stayed live. It's deliberately not built, because a background Python process reading the camera on a company-managed Mac isn't worth the policy risk. Outside OBS, the Mac stays open-loop.
