"""LinuxCameraProbe.listen() with real inotify on a temporary stand-in for /dev."""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from keylight.camera_linux import LinuxCameraProbe


@unittest.skipUnless(sys.platform.startswith("linux"), "inotify is Linux-only")
class InotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.dev = root / "dev"
        (self.dev / "v4l" / "by-id").mkdir(parents=True)
        (self.dev / "video0").touch()
        (self.dev / "tty1").touch()
        self.changes = threading.Semaphore(0)
        self.probe = LinuxCameraProbe(["usb-Card-video-index0"], dev_root=str(self.dev),
                                      proc_root=str(root / "proc"), sys_root=str(root / "sys"))
        self.assertTrue(self.probe.listen(self.changes.release))

    def tearDown(self):
        self.probe.stop_listening()
        self.tmp.cleanup()

    def changed(self, timeout=2.0):
        return self.changes.acquire(timeout=timeout)

    def drain(self):
        while self.changes.acquire(timeout=0.15):
            pass

    def test_open_and_close_of_a_video_node(self):
        fd = os.open(self.dev / "video0", os.O_RDONLY)
        self.assertTrue(self.changed())
        self.drain()
        os.close(fd)
        self.assertTrue(self.changed())

    def test_unrelated_dev_entries_are_ignored(self):
        (self.dev / "tty2").touch()
        with open(self.dev / "tty1"):
            pass
        self.assertFalse(self.changed(0.3))

    def test_new_node_is_watched(self):
        # KVM switch back: the card comes back as a new node
        (self.dev / "video3").touch()
        self.assertTrue(self.changed())
        self.drain()
        with open(self.dev / "video3"):
            pass
        self.assertTrue(self.changed())

    def test_by_id_link_appearing_wakes(self):
        (self.dev / "v4l" / "by-id" / "usb-Card-video-index0").symlink_to(self.dev / "video0")
        self.assertTrue(self.changed())

    def test_v4l_tree_recreated(self):
        import shutil
        shutil.rmtree(self.dev / "v4l")
        self.assertTrue(self.changed())
        self.drain()
        (self.dev / "v4l" / "by-id").mkdir(parents=True)
        self.drain()
        (self.dev / "v4l" / "by-id" / "usb-Card-video-index0").symlink_to(self.dev / "video0")
        self.assertTrue(self.changed())

    def test_own_scans_do_not_trigger_events(self):
        (self.dev / "v4l" / "by-id" / "usb-Card-video-index0").symlink_to(self.dev / "video0")
        self.drain()
        for _ in range(3):
            self.probe.poll()
            self.probe.describe()
        self.assertFalse(self.changed(0.3))


class InotifyUnavailableTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "inotify is Linux-only")
    def test_unwatchable_dev_returns_false(self):
        self.assertFalse(LinuxCameraProbe([], dev_root="/nonexistent/dev").listen(lambda: None))

    def test_listen_returns_false(self):
        from unittest import mock
        from keylight import camera_linux
        probe = LinuxCameraProbe([], dev_root="/nonexistent")
        with mock.patch.object(camera_linux, "_Inotify", side_effect=OSError(24, "too many")):
            self.assertFalse(probe.listen(lambda: None))


if __name__ == "__main__":
    unittest.main()
