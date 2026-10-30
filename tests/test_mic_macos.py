"""MicUsers with a fake CoreAudio (runs on any OS)."""

import unittest

from keylight.core import WatchConfig
from keylight.mic_macos import MicUsers

OBS, REPLAYD, ZOOM, CHROME_HELPER, DICTATION, GRANOLA, MUSIC = range(20, 27)


class FakeCoreAudio:
    def __init__(self):
        self.bundles = {OBS: "com.obsproject.obs-studio", REPLAYD: "com.apple.replayd",
                        ZOOM: "us.zoom.xos", CHROME_HELPER: "com.google.Chrome.helper",
                        DICTATION: "com.apple.CoreSpeech", GRANOLA: "com.granola.app.helper",
                        MUSIC: "com.apple.Music"}
        self.capturing = {OBS, REPLAYD}
        self.id_reads = 0
        self.flag_reads: list[int] = []

    def processes(self):
        return list(self.bundles)

    def running_input(self, proc):
        self.flag_reads.append(proc)
        return proc in self.capturing

    def bundle_id(self, proc):
        self.id_reads += 1
        return self.bundles[proc]


def make_users(api, call_apps=None):
    m = MicUsers.__new__(MicUsers)
    m.call_apps = tuple(a.lower() for a in (call_apps if call_apps is not None else WatchConfig().mic_call_apps))
    m.api = api
    m._bundles = {}
    return m


class MicUsersTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeCoreAudio()
        self.m = make_users(self.api)

    def test_obs_and_screen_capture_dont_count(self):
        self.assertEqual(self.m(), [])

    def test_call_app_counts(self):
        self.api.capturing.add(ZOOM)
        self.assertEqual(self.m(), ["us.zoom.xos"])

    def test_browser_helper_counts(self):
        # Chrome captures for a Meet tab in its helper process
        self.api.capturing.add(CHROME_HELPER)
        self.assertEqual(self.m(), ["com.google.Chrome.helper"])

    def test_dictation_and_meeting_recorder_dont_count(self):
        self.api.capturing |= {DICTATION, GRANOLA}
        self.assertEqual(self.m(), [])

    def test_capturing_lists_everything_for_probe(self):
        self.api.capturing |= {ZOOM, GRANOLA}
        self.assertEqual(sorted(self.m.capturing()),
                         ["com.apple.replayd", "com.granola.app.helper", "com.obsproject.obs-studio", "us.zoom.xos"])

    def test_prefix_match_is_case_insensitive(self):
        self.api.bundles[ZOOM] = "US.Zoom.XOS"
        self.api.capturing.add(ZOOM)
        self.assertEqual(self.m(), ["US.Zoom.XOS"])

    def test_custom_list_replaces_default(self):
        self.api.capturing |= {ZOOM, GRANOLA}
        self.assertEqual(make_users(self.api, ["com.granola."])(), ["com.granola.app.helper"])

    def test_only_call_app_flags_are_read(self):
        # reading a flag costs ~0.23 ms, so the ~40 non-call processes are skipped
        self.m()
        self.assertEqual(sorted(self.api.flag_reads), [ZOOM, CHROME_HELPER])

    def test_bundle_ids_are_cached_and_new_processes_looked_up(self):
        self.m()
        first = self.api.id_reads
        self.m()
        self.assertEqual(self.api.id_reads, first)          # nothing new: no lookups
        new = 30
        self.api.bundles[new] = "com.microsoft.teams2"
        self.api.capturing.add(new)
        self.assertEqual(self.m(), ["com.microsoft.teams2"])
        self.assertEqual(self.api.id_reads, first + 1)

    def test_cache_drops_finished_processes(self):
        self.m()
        del self.api.bundles[ZOOM]
        self.m()
        self.assertNotIn(ZOOM, self.m._bundles)


if __name__ == "__main__":
    unittest.main()
