"""MacCameraProbe decision logic with a fake CoreMediaIO (runs on any OS)."""

import threading
import unittest
from unittest import mock

from keylight import camera_macos
from keylight.camera_macos import HW_DEVICES, PROP_NAME, PROP_RUNNING_SOMEWHERE, SYSTEM_OBJECT, MacCameraProbe
from keylight.core import Observation

CARD, FACETIME, VIRTUAL = 10, 11, 12


class FakeCMIO:
    def __init__(self):
        self.present = {CARD: "Live Streamer CAP 4K", FACETIME: "FaceTime HD Camera"}
        self.running: set[int] = set()
        self.listeners: dict = {}

    def devices(self):
        return list(self.present)

    def string(self, dev, prop):
        return self.present[dev] if prop == PROP_NAME else f"uid-{dev}"

    def running_somewhere(self, dev):
        return dev in self.running

    def add_listener(self, obj, selector, fn):
        self.listeners.setdefault((obj, selector), []).append(fn)
        return True

    def fire(self, obj, selector):
        for fn in self.listeners.get((obj, selector), []):
            fn()


def make_probe(api, hub_holds_camera=lambda: None, virtual=(), mic_users=None):
    """A MacCameraProbe on a fake CoreMediaIO (skips the real _CMIO())."""
    p = MacCameraProbe.__new__(MacCameraProbe)
    p.specs = ["live streamer cap 4k"]
    p.virtual_specs = [v.lower() for v in virtual]
    p.any_camera = False
    p.hub_holds_camera = hub_holds_camera
    p.mic_users = mic_users
    p._mic_watch = False
    p.api = api
    p._on_change = None
    p._watched = set()
    p._lock = threading.Lock()
    return p


class MacProbeTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeCMIO()
        self.obs_answer = None
        self.pgrep = mock.patch.object(camera_macos, "obs_process_running", return_value=False)
        self.pgrep_mock = self.pgrep.start()

    def tearDown(self):
        self.pgrep.stop()

    def probe(self, virtual=(), mic_users=None):
        return make_probe(self.api, lambda: self.obs_answer, virtual, mic_users)

    def test_idle_and_direct_use(self):
        p = self.probe()
        self.assertIs(p.poll(), Observation.IDLE)
        self.api.running.add(CARD)
        self.assertIs(p.poll(), Observation.IN_USE)

    def test_obs_camera_source_live_hides_card_use(self):
        self.api.running.add(CARD)
        self.obs_answer = True
        self.assertIs(self.probe().poll(), Observation.IDLE)

    def test_obs_open_without_live_camera_source_does_not_hide_card_use(self):
        # OBS open for a screen recording during a call: the call still counts
        self.api.running.add(CARD)
        self.obs_answer = False
        self.pgrep_mock.return_value = True
        self.assertIs(self.probe().poll(), Observation.IN_USE)

    def test_no_websocket_falls_back_to_process_check(self):
        self.api.running.add(CARD)
        self.obs_answer = None
        self.pgrep_mock.return_value = True
        self.assertIs(self.probe().poll(), Observation.IDLE)
        self.pgrep_mock.return_value = False
        self.assertIs(self.probe().poll(), Observation.IN_USE)

    def test_call_next_to_live_obs_counts_when_another_app_has_the_mic(self):
        # OBS shows the card, and Zoom reads it too: only the mic tells them apart
        self.api.running.add(CARD)
        self.obs_answer = True
        mic = []
        p = self.probe(mic_users=lambda: mic)
        self.assertIs(p.poll(), Observation.IDLE)          # OBS alone (its own mic is filtered out)
        mic.append("us.zoom.xos")
        self.assertIs(p.poll(), Observation.IN_USE)        # call joined
        mic.clear()
        self.assertIs(p.poll(), Observation.IDLE)          # call left, OBS still open

    def test_mic_is_polled_only_while_obs_holds_the_card(self):
        # CoreAudio can't push input changes, so the probe asks to be re-polled,
        # but only in the one state where the mic decides the answer
        mic = []
        p = self.probe(mic_users=lambda: mic)
        p.poll()
        self.assertIsNone(p.next_wake(0))                   # card idle
        self.api.running.add(CARD)
        self.obs_answer = False
        p.poll()
        self.assertIsNone(p.next_wake(0))                   # call with OBS closed: camera push covers it
        self.obs_answer = True
        p.poll()
        self.assertEqual(p.next_wake(0), MacCameraProbe.MIC_POLL)
        mic.append("us.zoom.xos")
        self.assertIs(p.poll(), Observation.IN_USE)
        self.assertEqual(p.next_wake(0), MacCameraProbe.MIC_POLL)   # keep watching, to see the call end
        self.api.running.clear()
        p.poll()
        self.assertIsNone(p.next_wake(0))                   # OBS stopped too

    def test_no_mic_check_no_polling(self):
        self.api.running.add(CARD)
        self.obs_answer = True
        p = self.probe()
        p.poll()
        self.assertIsNone(p.next_wake(0))

    def test_mic_alone_is_not_a_session(self):
        # a voice-only call (or dictation) with the camera off doesn't turn the light on
        self.obs_answer = True
        self.assertIs(self.probe(mic_users=lambda: ["us.zoom.xos"]).poll(), Observation.IDLE)

    def test_other_camera_ignored(self):
        self.api.running.add(FACETIME)
        self.assertIs(self.probe().poll(), Observation.IDLE)

    def test_absent(self):
        del self.api.present[CARD]
        self.assertIs(self.probe().poll(), Observation.ABSENT)

    def test_virtual_camera_use_counts(self):
        self.api.present[VIRTUAL] = "OBS Virtual Camera"
        self.api.running.add(VIRTUAL)
        self.obs_answer = True
        self.assertIs(self.probe(virtual=["OBS Virtual Camera"]).poll(), Observation.IN_USE)


class MacListenTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeCMIO()
        self.changes = 0
        self.p = make_probe(self.api)

    def changed(self):
        self.changes += 1

    def test_running_change_on_any_device_notifies(self):
        self.assertTrue(self.p.listen(self.changed))
        self.api.fire(CARD, PROP_RUNNING_SOMEWHERE)
        self.api.fire(FACETIME, PROP_RUNNING_SOMEWHERE)
        self.assertEqual(self.changes, 2)

    def test_device_that_arrives_later_gets_a_listener(self):
        # The KVM case: the card is away when the watcher starts and comes back later.
        del self.api.present[CARD]
        self.p.listen(self.changed)
        self.assertNotIn((CARD, PROP_RUNNING_SOMEWHERE), self.api.listeners)
        self.api.present[CARD] = "Live Streamer CAP 4K"
        self.api.fire(SYSTEM_OBJECT, HW_DEVICES)
        self.assertEqual(self.changes, 1)
        self.api.fire(CARD, PROP_RUNNING_SOMEWHERE)
        self.assertEqual(self.changes, 2)

    def test_device_list_change_does_not_add_duplicate_listeners(self):
        self.p.listen(self.changed)
        self.api.fire(SYSTEM_OBJECT, HW_DEVICES)
        self.assertEqual(len(self.api.listeners[(CARD, PROP_RUNNING_SOMEWHERE)]), 1)

    def test_refused_listener_reports_failure(self):
        self.api.add_listener = lambda *_: False
        self.assertFalse(self.p.listen(self.changed))


if __name__ == "__main__":
    unittest.main()
