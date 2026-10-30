import logging
import threading
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from keylight.core import KeyLight, Observation, parse_config
from keylight.watch import Event, ObsSessionProbe, SessionProbe, SessionTracker, Watcher

from .mock_light import MockLight

IN, IDLE, GONE = Observation.IN_USE, Observation.IDLE, Observation.ABSENT


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.t = SessionTracker(on_delay=2, off_delay=5, absent_grace=15)

    def feed(self, *steps):
        """steps: (time, observation); returns events, committing each one."""
        events = []
        for now, obs in steps:
            ev = self.t.update(now, obs)
            if ev:
                self.t.commit(ev)
                events.append((now, ev))
        return events

    def test_on_debounce(self):
        self.assertEqual(self.feed((0, IN), (1, IN), (2, IN)), [(2, Event.START)])

    def test_brief_open_ignored(self):
        self.assertEqual(self.feed((0, IN), (1, IDLE), (2, IN), (3, IDLE), (10, IDLE)), [])

    def test_off_debounce_tolerates_reopen(self):
        events = self.feed((0, IN), (2, IN), (3, IDLE), (6, IN), (7, IDLE), (11, IDLE), (12, IDLE))
        self.assertEqual(events, [(2, Event.START), (12, Event.STOP)])

    def test_absent_no_toggle_within_grace(self):
        events = self.feed((0, IN), (2, IN), (3, GONE), (10, GONE), (12, IN), (30, IN))
        self.assertEqual(events, [(2, Event.START)])
        self.assertTrue(self.t.active)

    def test_absent_past_grace_is_lost(self):
        events = self.feed((0, IN), (2, IN), (3, GONE), (17, GONE), (18, GONE))
        self.assertEqual(events, [(2, Event.START), (18, Event.LOST)])
        self.assertFalse(self.t.active)

    def test_absent_while_idle_does_nothing(self):
        self.assertEqual(self.feed((0, GONE), (100, GONE), (101, IDLE)), [])

    def test_next_deadline(self):
        self.assertIsNone(self.t.next_deadline(0))              # nothing pending
        self.t.update(0, IN)
        self.assertEqual(self.t.next_deadline(0.5), 1.5)         # on_delay=2
        self.feed((2, IN), (3, IDLE))
        self.assertEqual(self.t.next_deadline(4), 4)             # off_delay=5 from t=3
        self.feed((5, GONE))
        self.assertEqual(self.t.next_deadline(5), 15)            # absent_grace while active
        self.t.active = False
        self.assertIsNone(self.t.next_deadline(5))               # absent while idle: nothing to fire

    def test_uncommitted_event_repeats(self):
        self.assertIsNone(self.t.update(0, IN))
        self.assertEqual(self.t.update(2, IN), Event.START)
        self.assertEqual(self.t.update(3, IN), Event.START)    # not committed: retried


def wait_for(cond, timeout=2.0) -> bool:
    deadline = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if cond():
            return True
        deadline.wait(0.01)
    return bool(cond())


class FakeProbe:
    def __init__(self):
        self.obs = IDLE

    def poll(self):
        return self.obs


class WatcherTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.mock = MockLight().__enter__()
        self.now = 0.0
        self.probe = FakeProbe()
        self.make()

    def make(self, auto_factory=None, extra=None, **watch):
        cfg = parse_config({
            "presets": {"call": {"brightness": 60, "kelvin": 5000}},
            "watch": {"on_delay": 2, "off_delay": 5, "on_action": "preset:call", **watch},
            **(extra or {}),
        })
        light = KeyLight("127.0.0.1", self.mock.port, timeout=0.5)
        self.watcher = Watcher(cfg, self.probe, runner=lambda fn: fn(light), clock=lambda: self.now,
                               auto_factory=auto_factory,
                               now=lambda: datetime(2026, 6, 21, 13, 0, tzinfo=ZoneInfo("America/New_York")))

    def tearDown(self):
        self.mock.__exit__()
        logging.disable(logging.NOTSET)

    def advance(self, obs, until, step=1.0):
        self.probe.obs = obs
        while self.now < until:
            self.watcher.tick()
            self.now += step
        self.watcher.tick()

    def test_wait_time_polls_only_without_push(self):
        self.make(poll_interval=0.5)
        self.assertEqual(self.watcher.wait_time(), 0.5)
        self.watcher.pushed = True
        self.assertIsNone(self.watcher.wait_time())              # sleep until woken
        self.probe.obs = IN
        self.watcher.tick()                                     # on_delay=2 now pending
        self.assertAlmostEqual(self.watcher.wait_time(), 2.05)

    def test_wait_time_includes_probe_retry(self):
        self.probe.next_wake = lambda now: 4.0
        self.watcher.pushed = True
        self.assertAlmostEqual(self.watcher.wait_time(), 4.05)

    def test_push_wakes_the_loop(self):
        polls = []
        probe = self.probe

        class PushProbe:
            def listen(self, on_change):
                self.on_change = on_change
                return True

            def poll(self):
                polls.append(1)
                return probe.obs

        pushing = PushProbe()
        self.probe = pushing
        self.make(poll_interval=60)
        stop = threading.Event()
        t = threading.Thread(target=self.watcher.run, args=(stop,))
        t.start()
        try:
            self.assertTrue(wait_for(lambda: polls))
            self.assertTrue(self.watcher.pushed)
            before = len(polls)
            pushing.on_change()
            self.assertTrue(wait_for(lambda: len(polls) > before))
            self.assertGreater(len(polls), before)               # woke at once, though nothing is pending
        finally:
            stop.set()
            self.watcher.wake()
            t.join(2)
        self.assertFalse(t.is_alive())

    def test_call_cycle(self):
        self.advance(IN, 3)
        self.assertEqual(self.mock.state, {"on": 1, "brightness": 60, "temperature": 200})
        self.advance(IDLE, 10)
        self.assertEqual(self.mock.state["on"], 0)

    def test_auto_action_uses_sun_schedule_and_pinned_kelvin(self):
        self.make(on_action="auto", extra={
            "light": {"kelvin": 4700},
            "location": {"latitude": 40.71, "longitude": -74.01},
            "auto": {"day_brightness": 45, "night_brightness": 20},
        })
        self.advance(IN, 3)
        self.assertEqual(self.mock.state, {"on": 1, "brightness": 45, "temperature": 213})

    def test_auto_brightness_runs_for_the_session(self):
        events = []

        class FakeAuto:
            def start(self):
                events.append("start")

            def stop(self):
                events.append("stop")

        self.make(auto_factory=FakeAuto)
        self.mock.state["on"] = 1                    # not Owned: auto still runs
        self.advance(IN, 3)
        self.assertEqual(events, ["start"])
        self.advance(IDLE, 10)
        self.assertEqual(events, ["start", "stop"])
        self.assertEqual(self.mock.state["on"], 1)

    def test_restore_action(self):
        self.make(on_action="restore")
        self.advance(IN, 3)
        self.assertEqual(self.mock.state, {"on": 1, "brightness": 40, "temperature": 213})

    def test_light_already_on_is_left_alone(self):
        self.mock.state["on"] = 1
        self.advance(IN, 3)
        self.advance(IDLE, 10)
        self.assertEqual(self.mock.state["on"], 1)
        self.assertEqual(self.mock.puts(), [])

    def test_already_on_gets_pinned_kelvin_only(self):
        self.make(extra={"light": {"kelvin": 4700}})
        self.mock.state.update(on=1, brightness=13, temperature=234)
        self.advance(IN, 3)
        self.assertEqual(self.mock.state, {"on": 1, "brightness": 13, "temperature": 213})
        self.assertEqual(self.mock.puts(), [{"temperature": 213}])
        self.advance(IDLE, 10)
        self.assertEqual(self.mock.state["on"], 1)          # still not Owned

    def test_manual_change_not_fought(self):
        self.advance(IN, 3)
        self.mock.state["brightness"] = 90          # user adjusts mid-call
        self.advance(IN, 30)
        self.assertEqual(self.mock.state["brightness"], 90)
        self.assertEqual(len(self.mock.puts()), 1)

    def test_kvm_switch_does_not_toggle(self):
        self.advance(IN, 3)
        self.advance(GONE, 10)
        self.assertEqual(self.mock.state["on"], 1)
        self.advance(IN, 20)
        self.advance(IDLE, 30)
        self.assertEqual(self.mock.state["on"], 0)     # still owned: turned off at the end

    def test_long_absence_forgets_session(self):
        self.advance(IN, 3)
        self.advance(GONE, 30)
        self.advance(IDLE, 40)
        self.assertEqual(self.mock.state["on"], 1)     # another machine may own it now

    def test_retries_when_unreachable(self):
        self.mock.fail_next = 4                         # two ticks' worth of attempts
        self.advance(IN, 6)
        self.assertEqual(self.mock.state["on"], 1)

    def test_probe_exception_is_survived_and_logged_once(self):
        class Broken:
            def poll(self):
                raise OSError("boom")
        self.watcher.probe = Broken()
        logging.disable(logging.NOTSET)
        with self.assertLogs("keylight-watch", level="WARNING") as cm:
            self.watcher.tick()
            self.watcher.tick()
            logging.getLogger("keylight-watch").warning("sentinel")
        self.assertEqual(sum("session probe failed" in line for line in cm.output), 1)


class FakeObsClient:
    active = False
    fail = False
    camera_live = False
    sources: list[str] = ["Video Capture Device"]
    connects = 0

    def __init__(self, _cfg, events=0):
        FakeObsClient.connects += 1
        if FakeObsClient.fail:
            from keylight.obs import ObsUnavailable
            raise ObsUnavailable("refused")

    def recording_or_streaming(self):
        return FakeObsClient.active

    def has_camera_input(self):
        return bool(FakeObsClient.sources)

    def capture_source(self):
        from keylight.obs import ObsError
        if not FakeObsClient.sources:
            raise ObsError("no video capture input found in OBS")
        return FakeObsClient.sources[0]

    def source_active(self, name):
        return FakeObsClient.camera_live

    def close(self):
        pass


class SessionProbeTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        FakeObsClient.active = False
        FakeObsClient.fail = False
        FakeObsClient.camera_live = False
        FakeObsClient.sources = ["Video Capture Device"]
        FakeObsClient.connects = 0
        self.cam = FakeProbe()
        self.now = 0.0
        self.obs = ObsSessionProbe(parse_config({}), clock=lambda: self.now, client_factory=FakeObsClient)
        self.probe = SessionProbe(self.cam, self.obs)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_recording_is_a_session(self):
        self.assertIs(self.probe.poll(), IDLE)
        FakeObsClient.active = True
        self.assertIs(self.probe.poll(), IN)

    def test_screen_only_recording_is_not_a_session(self):
        FakeObsClient.active = True
        FakeObsClient.sources = []
        self.assertIs(self.probe.poll(), IDLE)

    def test_recording_counts_even_when_camera_absent(self):
        self.cam.obs = GONE
        FakeObsClient.active = True
        self.assertIs(self.probe.poll(), IN)

    def test_camera_decides_absence(self):
        self.cam.obs = GONE
        self.assertIs(self.probe.poll(), GONE)

    def test_camera_in_use_by_obs(self):
        self.assertIsNone(self.obs.camera_in_use_by_obs())          # not connected yet
        self.obs.poll()
        self.assertFalse(self.obs.camera_in_use_by_obs())
        FakeObsClient.camera_live = True
        self.assertTrue(self.obs.camera_in_use_by_obs())

    def test_screen_only_obs_does_not_hold_camera(self):
        FakeObsClient.sources = []
        FakeObsClient.camera_live = True
        self.obs.poll()
        self.assertFalse(self.obs.camera_in_use_by_obs())

    def test_obs_connects_even_while_camera_in_use(self):
        self.cam.obs = IN
        self.probe.poll()
        self.assertTrue(self.obs.connected)

    def test_obs_down_retries_after_backoff(self):
        FakeObsClient.fail = True
        self.assertIs(self.probe.poll(), IDLE)
        FakeObsClient.fail, FakeObsClient.active = False, True
        self.assertIs(self.probe.poll(), IDLE)       # still backing off
        self.now += ObsSessionProbe.RETRY
        self.assertIs(self.probe.poll(), IN)


class FakeEvents:
    def __init__(self, _cfg, on_change):
        self.on_change = on_change
        self.alive = True
        self.closed = False

    def close(self):
        self.closed = True


class PushedObsProbeTests(unittest.TestCase):
    """ObsSessionProbe after listen(): connects only when kicked, with backoff."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        FakeObsClient.active = False
        FakeObsClient.fail = False
        FakeObsClient.sources = ["Video Capture Device"]
        FakeObsClient.connects = 0
        self.now = 0.0
        self.events = []
        self.obs = ObsSessionProbe(parse_config({}), clock=lambda: self.now, client_factory=FakeObsClient,
                                   events_factory=lambda cfg, cb: self.events.append(FakeEvents(cfg, cb))
                                   or self.events[-1])

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_connects_at_listen_and_opens_event_stream(self):
        self.obs.listen(lambda: None)
        FakeObsClient.active = True
        self.assertIs(self.obs.poll(), IN)
        self.assertEqual(len(self.events), 1)

    def test_backoff_then_gives_up_until_kicked(self):
        FakeObsClient.fail = True
        self.obs.listen(lambda: None)
        waits = []
        for _ in range(10):
            self.obs.poll()
            due = self.obs.next_wake(self.now)
            if due is None:
                break
            waits.append(due)
            self.now += due
        self.assertEqual(waits, list(ObsSessionProbe.BACKOFF))
        self.assertEqual(FakeObsClient.connects, 1 + len(ObsSessionProbe.BACKOFF))
        self.now += 1000
        self.obs.poll()
        self.assertEqual(FakeObsClient.connects, 1 + len(ObsSessionProbe.BACKOFF))   # quiet: no polling
        FakeObsClient.fail = False
        self.obs.kick()                                  # a camera changed: OBS may have started
        FakeObsClient.active = True
        self.assertIs(self.obs.poll(), IN)

    def test_backoff_succeeds_when_obs_comes_up(self):
        FakeObsClient.fail = True
        self.obs.listen(lambda: None)
        self.obs.poll()
        FakeObsClient.fail, FakeObsClient.active = False, True
        self.assertIs(self.obs.poll(), IDLE)             # not due yet
        self.now += ObsSessionProbe.BACKOFF[0]
        self.assertIs(self.obs.poll(), IN)
        self.assertIsNone(self.obs.next_wake(self.now))

    def test_obs_event_schedules_settle_rereads(self):
        # OBS still reports the recording active right after RecordStateChanged STOPPED
        self.obs.listen(lambda: None)
        FakeObsClient.active = True
        self.assertIs(self.obs.poll(), IN)
        self.events[-1].on_change()                      # STOPPED arrives; status hasn't caught up
        self.assertIs(self.obs.poll(), IN)
        self.assertEqual(self.obs.next_wake(self.now), ObsSessionProbe.SETTLE[0])
        FakeObsClient.active = False                     # caught up
        rereads = []
        while (due := self.obs.next_wake(self.now)) is not None:
            self.now += due + 0.05
            rereads.append(self.obs.poll())
        self.assertEqual(rereads, [IDLE] * len(ObsSessionProbe.SETTLE))

    def test_dead_event_stream_disconnects_and_waits_for_kick(self):
        self.obs.listen(lambda: None)
        self.obs.poll()
        self.events[-1].alive = False                    # OBS quit
        FakeObsClient.fail = True
        self.assertIs(self.obs.poll(), IDLE)
        self.assertFalse(self.obs.connected)
        self.assertTrue(self.events[-1].closed)
        self.assertIsNone(self.obs.next_wake(self.now))

    def test_camera_change_kicks_obs(self):
        class Cam(FakeProbe):
            def listen(self, cb):
                self.cb = cb
                return True
        cam = Cam()
        probe = SessionProbe(cam, self.obs)
        FakeObsClient.fail = True
        self.assertTrue(probe.listen(lambda: None))
        for _ in range(10):                             # exhaust the backoff
            probe.poll()
            self.now += 100
        before = FakeObsClient.connects
        FakeObsClient.fail = False
        cam.cb()
        probe.poll()
        self.assertEqual(FakeObsClient.connects, before + 1)
        self.assertTrue(self.obs.connected)

    def test_no_camera_push_means_obs_polls(self):
        probe = SessionProbe(FakeProbe(), self.obs)
        self.assertFalse(probe.listen(lambda: None))
        self.assertFalse(self.obs.pushed)


class EntryPointTests(unittest.TestCase):
    def test_module_rerun_shares_observation(self):
        # `python -m keylight.watch` executes watch.py a second time as __main__;
        # the probe's Observation must still be the class the tracker compares to.
        import runpy
        from keylight import camera_linux
        ns = runpy.run_module("keylight.watch", run_name="__not_main__")
        self.assertIs(ns["Observation"], camera_linux.Observation)


if __name__ == "__main__":
    unittest.main()
