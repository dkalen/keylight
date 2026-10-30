"""`keylight-watch`: keep the Light on for exactly as long as a Session lasts.

A Session is any of: a non-Hub app reading the camera, an app reading the Hub's
virtual camera, or OBS recording/streaming with the camera set up as one of its
sources (see GLOSSARY.md).

Rules:
  - Session for `on_delay` s: if the Light is off, apply `on_action` and remember
    that we turned it on (Owned). If it was already on, only bring it to the
    pinned colour temperature.
  - Session over for `off_delay` s: turn the Light off, but only if Owned.
  - Auto-brightness (if enabled) runs for the whole Session, Owned or not.
  - We act on transitions only, so manual changes are never fought.
  - Camera vanishes (KVM switch): no light change. If it stays gone longer than
    `absent_grace`, the Session is forgotten (another machine may own the light now).

Changes are pushed, not polled: CoreMediaIO listeners (macOS, delivered via the
main thread's run loop) or inotify on the video nodes and /dev (Linux), plus a
second, events-only OBS websocket connection. Every wake re-reads the real
state, so a missed event only delays things until the next one. Between events
the loop sleeps, waking only for pending on/off/absent deadlines and OBS
connection retries. If the camera can't notify, it polls every `poll_interval` s.
"""

from __future__ import annotations

import argparse
import enum
import logging
import signal
import sys
import threading
import time
from typing import Callable

from datetime import datetime

from .core import (Config, KeyLight, KeylightError, Observation, Preset, clamp, kelvin_to_mired, load_config,
                   run_with_fallback)
from .sun import schedule_brightness

log = logging.getLogger("keylight-watch")


class Event(enum.Enum):
    START = "start"
    STOP = "stop"
    LOST = "lost"


class SessionTracker:
    """Debounces raw observations into session start/stop/lost events.

    An event is repeated on every update until `commit()` is called, so a
    failed light action is retried on the next poll."""

    def __init__(self, on_delay: float, off_delay: float, absent_grace: float):
        self.on_delay = on_delay
        self.off_delay = off_delay
        self.absent_grace = absent_grace
        self.active = False
        self._want: bool | None = None
        self._since = 0.0
        self._absent_since: float | None = None

    def update(self, now: float, obs: Observation) -> Event | None:
        if obs is Observation.ABSENT:
            self._want = None
            if self._absent_since is None:
                self._absent_since = now
            if self.active and now - self._absent_since >= self.absent_grace:
                return Event.LOST
            return None
        self._absent_since = None
        want = obs is Observation.IN_USE
        if want == self.active:
            self._want = None
            return None
        if self._want != want:
            self._want, self._since = want, now
        if now - self._since >= (self.on_delay if want else self.off_delay):
            return Event.START if want else Event.STOP
        return None

    def next_deadline(self, now: float) -> float | None:
        """Seconds until a pending start/stop/lost would fire, if one is pending."""
        if self._absent_since is not None:
            return max(0.0, self._absent_since + self.absent_grace - now) if self.active else None
        if self._want is None:
            return None
        return max(0.0, self._since + (self.on_delay if self._want else self.off_delay) - now)

    def commit(self, event: Event) -> None:
        self.active = event is Event.START
        self._want = None


class ObsSessionProbe:
    """IN_USE while OBS is recording or streaming with the Camera set up as one
    of its sources; IDLE otherwise (screen-only setups, OBS not running).

    Pushed mode (after `listen()`): a second connection delivers OBS's events, and
    OBS is only (re)connected after `kick()` (a camera changed: OBS may have just
    started), retrying with BACKOFF and then giving up until the next kick.
    Otherwise (one-off `--probe`, or no camera notifications) it retries every RETRY s."""
    RETRY = 10.0
    BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0)
    # Re-read OBS this long after each of its events: its status lags the event
    # (GetRecordStatus still says active ~0.5 s after RecordStateChanged STOPPED).
    SETTLE = (1.0, 3.0, 7.0)

    def __init__(self, cfg: Config, clock: Callable[[], float] = time.monotonic, client_factory=None,
                 events_factory=None):
        self.cfg = cfg
        self.clock = clock
        if client_factory is None:
            from .obs import ObsClient
            client_factory = ObsClient
        if events_factory is None:
            from .obs import ObsEventListener
            events_factory = lambda cfg, on_change: ObsEventListener(cfg, on_change, client_factory)  # noqa: E731
        self.client_factory = client_factory
        self.events_factory = events_factory
        self.client = None
        self.events = None
        self._source: str | None = None
        self._next_try = 0.0
        self._reason: str | None = None
        self._on_change: Callable[[], None] | None = None
        self._kicked = False
        self._retry_at: float | None = None
        self._attempt = 0
        self._event_seen = False
        self._settle_at: list[float] = []

    @property
    def connected(self) -> bool:
        return self.client is not None

    @property
    def pushed(self) -> bool:
        return self._on_change is not None

    def listen(self, on_change: Callable[[], None]) -> bool:
        self._on_change = on_change
        self.kick()                  # OBS may already be running
        return True

    def kick(self) -> None:
        """A camera changed. Safe from any thread: only sets a flag for poll()."""
        self._kicked = True

    def _obs_event(self) -> None:
        """From the event thread: only flags it for poll()."""
        self._event_seen = True
        self._on_change()

    def next_wake(self, now: float) -> float | None:
        """Seconds until the next connection retry or settle re-read, if any."""
        times = list(self._settle_at)
        if self.client is None and self._retry_at is not None:
            times.append(self._retry_at)
        return max(0.0, min(times) - now) if times else None

    def _note(self, reason: str | None, level: int = logging.DEBUG) -> None:
        if reason != self._reason:
            if reason:
                log.log(level, "OBS: %s", reason)
            elif self._reason:
                log.info("OBS websocket connected")
            self._reason = reason

    def _due(self) -> bool:
        now = self.clock()
        if not self.pushed:
            if now < self._next_try:
                return False
            self._next_try = now + self.RETRY
            return True
        if self._kicked:
            self._kicked = False
            self._attempt, self._retry_at = 0, now
        return self._retry_at is not None and now >= self._retry_at

    def _connect(self) -> bool:
        from .obs import ObsError, ObsUnavailable
        if not self._due():
            return False
        try:
            self.client = self.client_factory(self.cfg.obs)
            if self.pushed:
                self.events = self.events_factory(self.cfg.obs, self._obs_event)
        except (ObsUnavailable, ObsError) as e:
            self._disconnect()
            self._note(str(e), logging.WARNING if isinstance(e, ObsError) else logging.DEBUG)
            self._schedule_retry()
            return False
        self._retry_at = None
        self._note(None)
        return True

    def _schedule_retry(self) -> None:
        """Next step of BACKOFF (pushed mode), or give up until the next kick. The
        attempt count only resets on a kick or a successful request, so an OBS that
        accepts connections but can't answer yet (still starting) keeps being retried."""
        if not self.pushed:
            return
        if self._attempt < len(self.BACKOFF):
            self._retry_at = self.clock() + self.BACKOFF[self._attempt]
            self._attempt += 1
        else:
            self._retry_at = None
            log.debug("OBS: not usable; waiting for the next camera change")

    def _disconnect(self) -> None:
        for conn in (self.events, self.client):
            if conn is not None:
                conn.close()
        self.client = self.events = None
        self._source = None

    def poll(self) -> Observation:
        from .obs import ObsError, ObsUnavailable
        now = self.clock()
        if self._event_seen:
            self._event_seen = False
            self._settle_at = [now + d for d in self.SETTLE]
        self._settle_at = [t for t in self._settle_at if t > now]
        if self.client is not None and self.events is not None and not self.events.alive:
            self._disconnect()       # OBS quit or dropped us; reconnect only on a camera change
        if self.client is None and not self._connect():
            return Observation.IDLE
        try:
            session = self.client.recording_or_streaming() and self.client.has_camera_input()
        except (ObsUnavailable, ObsError) as e:
            self._note(str(e), logging.INFO)
            self._disconnect()
            self._schedule_retry()     # e.g. "not ready" while OBS is still starting up
            return Observation.IDLE
        self._attempt = 0
        return Observation.IN_USE if session else Observation.IDLE

    def camera_in_use_by_obs(self) -> bool | None:
        """Is OBS's own camera source live right now? None if OBS isn't reachable.
        No capture input at all (e.g. screen-only scenes) counts as False."""
        from .obs import ObsError, ObsUnavailable
        if self.client is None:
            return None
        try:
            if self._source is None:
                self._source = self.client.capture_source()
            return self.client.source_active(self._source)
        except ObsUnavailable:
            self._disconnect()
            return None
        except ObsError:
            self._source = None      # renamed/removed source, or no capture input
            return False

    def describe(self) -> str:
        if self.client is None:
            return f"obs={self._reason or 'not connected'}"
        from .obs import ObsError, ObsUnavailable
        try:
            cams = self.client.capture_inputs()
            live = {name: self.client.source_active(name) for name, _ in cams}
            recording = self.client.recording_or_streaming()
        except (ObsError, ObsUnavailable) as e:
            return f"obs=connected (query failed: {e})"
        listed = ", ".join(f"{n!r} [{k}] live={live[n]}" for n, k in cams) or "none"
        return f"obs=connected recording/streaming={recording} camera_inputs={listed}"


class SessionProbe:
    """Combines the camera probe with OBS recording/streaming. The camera decides
    presence (ABSENT); any source being in use makes a Session."""

    def __init__(self, camera, obs: ObsSessionProbe | None = None):
        self.camera = camera
        self.obs = obs
        self.any_camera = getattr(camera, "any_camera", False)

    def poll(self) -> Observation:
        # OBS first: it keeps the websocket connected, which the macOS camera
        # probe needs in order to ask whether OBS itself holds the camera.
        recording = self.obs is not None and self.obs.poll() is Observation.IN_USE
        cam = self.camera.poll()
        return Observation.IN_USE if recording else cam

    def describe(self) -> str:
        if self.obs is not None:
            self.obs.poll()
        parts = [self.camera.describe()]
        if self.obs is not None:
            parts.append(self.obs.describe())
        return "  ".join(parts)

    def all_devices(self) -> list[str]:
        return self.camera.all_devices()

    def listen(self, on_change: Callable[[], None]) -> bool:
        """Push mode: camera changes (which also prompt an OBS connection attempt,
        since OBS opening the camera is how we notice it starting) and OBS events
        wake the watcher. False if the camera can't notify; then everything polls."""
        def camera_changed() -> None:
            if self.obs is not None:
                self.obs.kick()
            on_change()

        listen = getattr(self.camera, "listen", None)
        if not (listen and listen(camera_changed)):
            return False
        if self.obs is not None:
            self.obs.listen(on_change)
        return True

    def next_wake(self, now: float) -> float | None:
        times = [t for t in (self.obs.next_wake(now) if self.obs is not None else None,
                             getattr(self.camera, "next_wake", lambda _: None)(now)) if t is not None]
        return min(times) if times else None


class Watcher:
    def __init__(self, cfg: Config, probe, dry_run: bool = False,
                 runner: Callable[[Callable[[KeyLight], object]], object] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 auto_factory: Callable[[], object] | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self.cfg = cfg
        self.probe = probe
        self.dry_run = dry_run
        self.clock = clock
        self.now = now
        self.runner = runner or (lambda fn: run_with_fallback(cfg, fn, warn=log.warning))
        self.auto_factory = auto_factory
        self.auto = None
        w = cfg.watch
        self.tracker = SessionTracker(w.on_delay, w.off_delay, w.absent_grace)
        self.owned = False
        self._failing = False
        self._last_obs: Observation | None = None
        self._probe_error: str | None = None
        self._wake = threading.Event()
        self.pushed = False

    def wake(self) -> None:
        """Run the next tick now (a probe pushed a change, or we're stopping)."""
        self._wake.set()

    def wait_time(self) -> float | None:
        """How long to sleep before the next tick; None means until woken. Pushed
        mode never polls: it wakes for events, pending on/off/absent deadlines
        and OBS connection retries only."""
        now = self.clock()
        times = [t + 0.05 for t in (self.tracker.next_deadline(now),
                                    getattr(self.probe, "next_wake", lambda _: None)(now)) if t is not None]
        if not self.pushed:
            times.append(self.cfg.watch.poll_interval)
        return min(times) if times else None

    def start_values(self) -> tuple[int | None, int | None]:
        """(brightness, mired) the Light should come on with, per on_action."""
        cfg, action = self.cfg, self.cfg.watch.on_action
        pinned = kelvin_to_mired(cfg.kelvin) if cfg.kelvin else None
        if action == "auto":
            a = cfg.auto
            b = schedule_brightness(self.now(), cfg.location.latitude, cfg.location.longitude,
                                    a.day_brightness, a.night_brightness)
            return clamp(b, a.min_brightness, a.max_brightness), pinned
        if action == "restore":
            return None, pinned          # the light remembers its last brightness
        preset: Preset = cfg.presets[action.removeprefix("preset:")]
        return preset.brightness, kelvin_to_mired(preset.kelvin) if preset.kelvin else pinned

    def _turn_on(self, light: KeyLight) -> bool:
        """Returns True if we turned the Light on (Owned). An already-on Light is
        left as is, except that it's brought to the pinned colour temperature
        (ADR 0002)."""
        state = light.get_state()
        if state.on:
            pinned = kelvin_to_mired(self.cfg.kelvin) if self.cfg.kelvin else None
            if pinned is not None and state.mired != pinned:
                light.set_state(mired=pinned)
                log.info("light already on: set colour temperature to pinned %d K", self.cfg.kelvin)
            return False
        brightness, mired = self.start_values()
        light.set_state(on=True, brightness=brightness, mired=mired)
        return True

    def _start_auto(self) -> None:
        if self.auto_factory and self.auto is None:
            self.auto = self.auto_factory()
            self.auto.start()

    def _stop_auto(self) -> None:
        if self.auto is not None:
            self.auto.stop()
            self.auto = None

    def handle(self, event: Event) -> bool:
        """Apply an event. Returns False if the light couldn't be reached (retry later)."""
        if event is Event.LOST:
            log.info("camera gone for %.0fs; forgetting session (light left as is)", self.cfg.watch.absent_grace)
            self._stop_auto()
            self.owned = False
            return True
        try:
            if event is Event.START:
                if self.dry_run:
                    b, m = self.start_values()
                    log.info("session started: would turn light on (%s: brightness=%s mired=%s)",
                             self.cfg.watch.on_action, b, m)
                    self.owned = True
                else:
                    self.owned = bool(self.runner(self._turn_on))
                    log.info("session started: %s", f"light on ({self.cfg.watch.on_action})"
                             if self.owned else "light was already on (not Owned; it stays on afterwards)")
                self._start_auto()
            else:
                self._stop_auto()
                if not self.owned:
                    log.info("session ended: light wasn't turned on by us, leaving it alone")
                elif self.dry_run:
                    log.info("session ended: would turn light off")
                else:
                    self.runner(lambda light: light.set_state(on=False))
                    log.info("session ended: light off")
                self.owned = False
        except KeylightError as e:
            if not self._failing:
                log.warning("%s (will keep retrying)", e)
            self._failing = True
            return False
        if self._failing:
            log.info("light reachable again")
        self._failing = False
        return True

    def tick(self) -> None:
        try:
            obs = self.probe.poll()
        except Exception as e:   # a probe glitch must never kill the watcher
            msg = f"{type(e).__name__}: {e}"
            if msg != self._probe_error:     # once per distinct error, not every poll
                log.warning("session probe failed: %s", msg)
                self._probe_error = msg
            return
        if self._probe_error:
            log.info("session probe working again")
            self._probe_error = None
        if obs is not self._last_obs:
            log.debug("session probe: %s", obs.value)
            if obs is Observation.ABSENT or self._last_obs is Observation.ABSENT:
                log.info("camera device %s", "disappeared" if obs is Observation.ABSENT else "is back")
            self._last_obs = obs
        event = self.tracker.update(self.clock(), obs)
        if event is not None and self.handle(event):
            self.tracker.commit(event)

    def run(self, stop: threading.Event) -> None:
        """Callers that set `stop` should also call `wake()` so the loop notices at once."""
        listen = getattr(self.probe, "listen", None)
        self.pushed = bool(listen and listen(self.wake))
        if self.pushed:
            log.info("camera and OBS changes are pushed; not polling")
        else:
            log.info("polling every %gs", self.cfg.watch.poll_interval)
        try:
            while not stop.is_set():
                self._wake.clear()
                self.tick()
                self._wake.wait(self.wait_time())
        finally:
            self._stop_auto()


def make_probe(cfg: Config) -> SessionProbe:
    w = cfg.watch
    obs = ObsSessionProbe(cfg) if w.obs_sessions else None
    if sys.platform == "darwin":
        from .camera_macos import MacCameraProbe
        mic_users = None
        if w.mic_sessions:
            from .mic_macos import MicUsers
            try:
                mic_users = MicUsers(w.mic_call_apps)
            except OSError as e:
                log.warning("microphone check unavailable (%s); a call while OBS shows the camera isn't detected", e)
        camera = MacCameraProbe(w.macos_devices, w.any_camera, w.macos_virtual_devices,
                                hub_holds_camera=obs.camera_in_use_by_obs if obs else None,
                                mic_users=mic_users)
    elif sys.platform.startswith("linux"):
        from .camera_linux import LinuxCameraProbe
        camera = LinuxCameraProbe(w.linux_devices, w.any_camera, w.ignore_processes,
                                  virtual_specs=w.linux_virtual_devices)
    else:
        raise KeylightError(f"unsupported platform: {sys.platform}")
    return SessionProbe(camera, obs)


def main(argv: list[str] | None = None) -> int:
    from . import log as logsetup

    p = argparse.ArgumentParser(prog="keylight-watch", description="Keep the Key Light on for the duration of each Session.")
    p.add_argument("--config", help="config file (default ~/.config/keylight/config.toml)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging (includes every probe change and auto cycle)")
    p.add_argument("--dry-run", action="store_true", help="log light actions instead of sending them")
    p.add_argument("--probe", action="store_true", help="print what the session probe sees right now and exit")
    p.add_argument("--list-devices", action="store_true", help="list cameras the probe can see and exit")
    p.add_argument("--no-auto", action="store_true", help="disable auto-brightness")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
        probe = make_probe(cfg)
    except (KeylightError, OSError) as e:
        print(f"keylight-watch: {e}", file=sys.stderr)
        return getattr(e, "exit_code", 1)

    if args.list_devices:
        print("\n".join(probe.all_devices()) or "no cameras found")
        return 0
    if args.probe:
        print(f"{probe.poll().value}  {probe.describe()}")
        return 0

    logsetup.setup("keylight-watch", args.verbose)
    w = cfg.watch
    devices = w.macos_devices + w.macos_virtual_devices if sys.platform == "darwin" else w.linux_devices + w.linux_virtual_devices
    auto_on = cfg.auto.enabled and not args.no_auto
    log.info("watching %s%s (on after %gs, off after %gs, light %s, auto-brightness %s)%s",
             "any camera" if probe.any_camera else devices, " + OBS recording/streaming" if probe.obs else "",
             w.on_delay, w.off_delay, cfg.host or "via mDNS", "on" if auto_on else "off",
             " [dry run]" if args.dry_run else "")

    runner = lambda fn: run_with_fallback(cfg, fn, warn=log.warning)  # noqa: E731
    auto_factory = None
    if auto_on:
        from .auto import AutoBrightness
        auto_factory = lambda: AutoBrightness(cfg, runner, dry_run=args.dry_run)  # noqa: E731

    stop = threading.Event()
    watcher = Watcher(cfg, probe, dry_run=args.dry_run, runner=runner, auto_factory=auto_factory)

    def on_signal(*_) -> None:
        stop.set()
        watcher.wake()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)
    if sys.platform == "darwin":
        # CoreMediaIO only notifies (and refreshes its device list) through the
        # main thread's run loop, so that runs here and the watch loop on a worker.
        from .camera_macos import run_main_loop

        def work() -> None:
            try:
                watcher.run(stop)
            finally:
                stop.set()

        worker = threading.Thread(target=work, name="watch")
        worker.start()
        run_main_loop(stop)
        worker.join()
    else:
        watcher.run(stop)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
