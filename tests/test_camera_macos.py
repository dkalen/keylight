"""MacCameraProbe decision logic with a fake CoreMediaIO (runs on any OS)."""

import unittest
from unittest import mock

from keylight import camera_macos
from keylight.camera_macos import PROP_NAME, PROP_UID, MacCameraProbe
from keylight.core import Observation

CARD, FACETIME, VIRTUAL = 10, 11, 12


class FakeCMIO:
    def __init__(self):
        self.present = {CARD: "Live Streamer CAP 4K", FACETIME: "FaceTime HD Camera"}
        self.running: set[int] = set()

    def devices(self):
        return list(self.present)

    def string(self, dev, prop):
        return self.present[dev] if prop == PROP_NAME else f"uid-{dev}"

    def running_somewhere(self, dev):
        return dev in self.running


class MacProbeTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeCMIO()
        self.obs_answer = None
        self.pgrep = mock.patch.object(camera_macos, "obs_process_running", return_value=False)
        self.pgrep_mock = self.pgrep.start()

    def tearDown(self):
        self.pgrep.stop()

    def probe(self, virtual=()):
        p = MacCameraProbe.__new__(MacCameraProbe)
        p.specs = ["live streamer cap 4k"]
        p.virtual_specs = [v.lower() for v in virtual]
        p.any_camera = False
        p.hub_holds_camera = lambda: self.obs_answer
        p.api = self.api
        return p

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


if __name__ == "__main__":
    unittest.main()
