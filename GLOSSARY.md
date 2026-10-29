# Key Light automation

Keeps an Elgato Key Light on, and correctly set, exactly while you're on camera, on whichever machine the KVM currently points at.

## Language

**Light**:
The single Elgato Key Light MK.2 this project controls.
_Avoid_: lamp, panel, device (reserve "device" for the camera)

**Camera**:
The capture card carrying the a6000's feed, as seen by the active machine.
_Avoid_: webcam, video device

**Session**:
A period when you're on camera: a non-Hub app reading the Camera directly, an app reading the Hub's virtual camera, or the Hub recording or streaming. The Light follows Sessions.
_Avoid_: call (a call is just one kind of Session), meeting, camera in use

**Hub**:
OBS, when it owns the Camera and re-publishes the feed as a virtual camera. Its own hold on the Camera never counts as a Session.
_Avoid_: OBS (when you mean the role rather than the program)

**Frame Source**:
Where auto-brightness gets pictures of you during a Session: the Hub, a shared stream alongside the call app, or none.
_Avoid_: feed, capture

**Closed-loop / Open-loop**:
Auto-brightness that corrects from measured Frames (closed-loop), versus following the Sun Schedule because there's no Frame Source (open-loop).

**Sun Schedule**:
The baseline brightness for a given moment, derived from the sun's position at your location.
_Avoid_: day/evening mode, time-of-day preset

**Camera White Balance**:
The fixed manual white balance set on the a6000. The Light's colour temperature is pinned to it so skin tones stay correct on camera.

**Preset**:
A named brightness level you define yourself and apply by hand. Nothing automatic uses Presets.
_Avoid_: scene, profile, mode

**Key Side**:
The half of your face nearest the Light. It's the part auto-brightness measures, because window light reaches mostly the other half.
_Avoid_: lit side, near side

**Owned**:
The state where the watcher itself turned the Light on for the current Session, which makes it responsible for turning the Light off when the Session ends. A Light that was already on when the Session started is never Owned.
_Avoid_: managed, controlled
