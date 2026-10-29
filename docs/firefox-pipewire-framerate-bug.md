# Firefox: PipeWire camera never links when the device's only frame rate is fractional

Bug report text, ready to file at https://bugzilla.mozilla.org (Core :: WebRTC: Audio/Video).

---

**Summary:** PipeWire camera: the stream requests an integer framerate (59/1) when the device only offers a fractional one (7013/117), so it never links. The site reports success and the video is blank.

**Environment:**
- Firefox 158.0.1, Fedora 44 package (`media.webrtc.camera.allow-pipewire = true`, the default)
- PipeWire with WirePlumber, GStreamer 1.28.8
- Capture device: AVerMedia Live Streamer CAP 4K (BU113, USB 07ca:1113, UVC), fed 1080p59.94 HDMI

**Steps to reproduce:**
1. Open https://mozilla.github.io/webrtc-landing/gum_test.html and click Camera.
2. Pick the capture card in the permission prompt.

**Expected:** live video.

**Actual:** the page says "Success!" but the video stays blank. Firefox's `Stream/Input/Video` node stays `suspended` and is never linked to the camera node.

**Details:**
- The camera node offers exactly two formats, both `1920x1080 @ 7013/117` (59.94 fps): `NV12` and `YUY2`.
- Firefox's stream node offers `EnumFormat: raw NV12 1920x1080 framerate 59/1`. That doesn't intersect with 7013/117, so negotiation fails.
- GStreamer confirms that the device only negotiates the exact fraction:
  ```
  pipewiresrc target-object=<serial> ! video/x-raw,format=NV12,width=1920,height=1080,framerate=7013/117 ! fakesink   # works
  ... framerate=59/1 / 60/1 / 30/1                                                                                     # fails
  ```
- Setting `media.webrtc.camera.allow-pipewire = false` (the V4L2 path) works with the same device.

The camera capability appears to be stored as an integer fps (truncated to 59), and then requested as an exact `59/1` rather than as the device's fraction or a range.

The closest related change upstream is libwebrtc's PipeWire framerate handling: https://webrtc.googlesource.com/src/+/3aa47cfd30dc965446cf1405bb062b756a62e6d1
