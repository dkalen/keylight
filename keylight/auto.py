"""Auto-brightness during a Session (Phase 2).

Closed-loop while a Frame Source is available: keep the Key Side of the face at
target luma, and within `max_stops_under` of the background. Open-loop (Sun
Schedule) otherwise. All control happens in stops, in linear light, with a
deadband, a per-update step limit and a slow fade, so there's no visible pulsing.

The control math is standard library; only the frame measurement (vision.py)
needs numpy/opencv.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from .core import AutoConfig, Config, KeyLight, KeylightError, LightState, clamp
from .sun import schedule_brightness

log = logging.getLogger("keylight-watch")


def srgb_to_linear(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


@dataclass
class Measurement:
    face_lin: float | None                 # Key Side mean, linear 0..1; None = no face
    bg_lin: float | None = None
    face_box: tuple[float, float, float, float] | None = None
    key_box: tuple[int, int, int, int] | None = None
    score: float | None = None


@dataclass
class Decision:
    mode: str                              # "closed-loop", "open-loop", "hold"
    current: int
    target: int
    error_stops: float | None = None
    note: str = ""


def stops_step(current: int, error_stops: float, cfg: AutoConfig) -> int:
    """Brightness after moving `error_stops` toward target, with gain, deadband and step limit."""
    if abs(error_stops) < cfg.deadband_stops:
        return current
    step = max(-cfg.max_step_stops, min(cfg.max_step_stops, cfg.gain * error_stops))
    new = round(max(current, 1) * 2 ** step)
    if new == current:                     # at low levels a fraction of a stop rounds to nothing
        new += 1 if step > 0 else -1
    return clamp(new, cfg.min_brightness, cfg.max_brightness)


def closed_loop(current: int, m: Measurement, cfg: AutoConfig) -> Decision:
    target_lin = srgb_to_linear(cfg.target_luma)
    note = ""
    if cfg.ratio_mode and m.bg_lin:
        floor = m.bg_lin / 2 ** cfg.max_stops_under
        if floor > target_lin:
            target_lin, note = floor, "ratio"
    err = math.log2(target_lin / max(m.face_lin or 0, 1e-5))
    return Decision("closed-loop", current, stops_step(current, err, cfg), err, note)


def open_loop(current: int, scheduled: int, cfg: AutoConfig) -> Decision:
    scheduled = clamp(scheduled, cfg.min_brightness, cfg.max_brightness)
    err = math.log2(scheduled / max(current, 1))
    target = stops_step(current, err, cfg)
    # never overshoot the schedule
    target = min(target, scheduled) if err > 0 else max(target, scheduled)
    return Decision("open-loop", current, target, err)


class FrameSource(Protocol):
    name: str

    def measure(self) -> Measurement: ...

    def close(self) -> None: ...


class ObsFrameSource:
    """Tier 1: screenshots of the Hub's camera source over obs-websocket."""
    name = "OBS"

    def __init__(self, cfg: Config):
        from .obs import ObsClient
        from .vision import Vision
        self.cfg = cfg
        self.vision = Vision(cfg.auto)
        self.client = ObsClient(cfg.obs)
        self.source = self.client.capture_source()

    def frame(self):
        return self.vision.decode(self.client.screenshot_jpeg(self.source, self.cfg.auto.screenshot_width))

    def measure(self) -> Measurement:
        return self.vision.measure(self.frame())

    def close(self) -> None:
        self.client.close()


def default_source_factory(cfg: Config) -> FrameSource | None:
    """Best available Frame Source, or None (open-loop). Raises with a reason."""
    return ObsFrameSource(cfg)


class AutoBrightness:
    """Runs on its own thread for the duration of one Session."""

    NO_FACE_LIMIT = 3          # consecutive frames without a face before falling back to open-loop
    SOURCE_RETRY = 30.0        # seconds between attempts to get a Frame Source

    def __init__(self, cfg: Config, runner: Callable[[Callable[[KeyLight], object]], object],
                 source_factory: Callable[[Config], FrameSource | None] = default_source_factory,
                 dry_run: bool = False, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self.cfg = cfg
        self.runner = runner
        self.source_factory = source_factory
        self.dry_run = dry_run
        self.clock = clock
        self.now = now
        self.source: FrameSource | None = None
        self._next_source_try = 0.0
        self._source_reason = ""
        self.mode: str | None = None
        self.last_sent: int | None = None
        self.paused_until = 0.0
        self.no_face = 0
        self.samples: deque[float] = deque(maxlen=3)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="auto-brightness", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._close_source()

    def _run(self) -> None:
        log.info("auto-brightness started%s", " [dry run]" if self.dry_run else "")
        while not self._stop.wait(self.cfg.auto.interval):
            try:
                self.cycle()
            except Exception:              # never let one bad cycle end the loop
                log.exception("auto-brightness cycle failed")
        log.info("auto-brightness stopped")

    # ---- frame source

    def _close_source(self) -> None:
        if self.source:
            try:
                self.source.close()
            except Exception:
                pass
            self.source = None

    def _get_source(self) -> FrameSource | None:
        if self.source is None and self.clock() >= self._next_source_try:
            self._next_source_try = self.clock() + self.SOURCE_RETRY
            try:
                self.source = self.source_factory(self.cfg)
            except (ImportError, OSError, KeylightError, ValueError) as e:
                reason = f"{type(e).__name__}: {e}" if isinstance(e, ImportError) else str(e)
                if reason != self._source_reason:
                    log.info("no frame source (%s)", reason)
                    self._source_reason = reason
        return self.source

    def _measure(self) -> Measurement | None:
        source = self._get_source()
        if source is None:
            return None
        try:
            return source.measure()
        except (OSError, KeylightError, ValueError) as e:
            log.info("frame source %s lost: %s", source.name, e)
            self._close_source()
            return None

    # ---- one control cycle

    def cycle(self) -> Decision | None:
        a = self.cfg.auto
        state: LightState = self.runner(lambda light: light.get_state())   # type: ignore[assignment]
        if self.last_sent is not None and (not state.on or state.brightness != self.last_sent):
            self.paused_until = self.clock() + a.manual_pause_min * 60
            self.last_sent = None
            log.info("manual change detected (now %s at %d%%); pausing auto-brightness for %.0f min",
                     "on" if state.on else "off", state.brightness, a.manual_pause_min)
        if self.clock() < self.paused_until or not state.on:
            return None

        m = self._measure()
        if m is not None and m.face_lin is None:
            self.no_face += 1
        elif m is not None:
            self.no_face = 0
        decision: Decision
        if m is not None and m.face_lin is not None:
            self.samples.append(m.face_lin)
            smoothed = Measurement(face_lin=statistics.median(self.samples), bg_lin=m.bg_lin)
            decision = closed_loop(state.brightness, smoothed, a)
            mode = f"closed-loop ({self.source.name if self.source else '?'})"
        elif m is not None and self.no_face < self.NO_FACE_LIMIT:
            decision = Decision("hold", state.brightness, state.brightness, note="no face")
            mode = self.mode or "closed-loop"
        else:
            self.samples.clear()
            scheduled = schedule_brightness(self.now(), self.cfg.location.latitude, self.cfg.location.longitude,
                                            a.day_brightness, a.night_brightness)
            decision = open_loop(state.brightness, scheduled, a)
            decision.note = f"schedule {scheduled}%"
            mode = "open-loop (Sun Schedule)"
        if mode != self.mode:
            log.info("auto-brightness: %s", mode)
            self.mode = mode

        level = logging.INFO if self.dry_run else logging.DEBUG
        face = f"{m.face_lin:.3f}" if m and m.face_lin is not None else "-"
        bg = f"{m.bg_lin:.3f}" if m and m.bg_lin is not None else "-"
        err = f"{decision.error_stops:+.2f}st" if decision.error_stops is not None else "-"
        log.log(level, "auto: %s face=%s bg=%s err=%s %d%% -> %d%% %s", decision.mode, face, bg, err,
                decision.current, decision.target, decision.note)

        if decision.target != decision.current and not self.dry_run:
            self.runner(lambda light: light.fade_to(brightness=decision.target, duration_ms=a.fade_ms))
            self.last_sent = decision.target
        elif not self.dry_run and self.last_sent is None:
            self.last_sent = state.brightness
        return decision
