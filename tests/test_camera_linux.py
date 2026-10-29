"""LinuxCameraProbe against a fake /dev, /proc and /sys tree."""

import os
import tempfile
import unittest
from pathlib import Path

from keylight.camera_linux import LinuxCameraProbe
from keylight.core import Observation

BY_ID = "usb-AVerMedia_Live_Streamer_CAP_4K_5312198300010-video-index0"


class FakeTree:
    def __init__(self, root: Path):
        self.root = root
        self.dev, self.proc, self.sys = root / "dev", root / "proc", root / "sys"
        (self.dev / "v4l" / "by-id").mkdir(parents=True)
        (self.sys / "class" / "video4linux").mkdir(parents=True)
        self.proc.mkdir()

    def add_video(self, n: int, name: str, by_id: str | None = None) -> Path:
        node = self.dev / f"video{n}"
        node.touch()
        d = self.sys / "class" / "video4linux" / f"video{n}"
        d.mkdir()
        (d / "name").write_text(name + "\n")
        if by_id:
            (self.dev / "v4l" / "by-id" / by_id).symlink_to(f"../../video{n}")
        return node

    def remove_all_video(self):
        for p in self.dev.glob("video*"):
            p.unlink()
        for p in (self.dev / "v4l" / "by-id").iterdir():
            p.unlink()
        for d in (self.sys / "class" / "video4linux").iterdir():
            (d / "name").unlink()
            d.rmdir()

    def add_process(self, pid: int, comm: str, *targets: Path):
        d = self.proc / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        for i, t in enumerate(targets):
            (d / "fd" / str(i + 3)).symlink_to(t)
        return d


class LinuxProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = FakeTree(Path(self.tmp.name).resolve())

    def tearDown(self):
        self.tmp.cleanup()

    def probe(self, specs, **kw):
        return LinuxCameraProbe(specs, dev_root=str(self.t.dev), proc_root=str(self.t.proc),
                                sys_root=str(self.t.sys), **kw)

    def test_absent(self):
        self.assertIs(self.probe(["usb-AVerMedia_*-video-index0"]).poll(), Observation.ABSENT)

    def test_idle_then_in_use(self):
        node = self.t.add_video(0, "Live Streamer CAP 4K", BY_ID)
        p = self.probe(["usb-AVerMedia_*-video-index0"])
        self.assertIs(p.poll(), Observation.IDLE)
        self.t.add_process(4242, "zoom", node)
        self.assertIs(p.poll(), Observation.IN_USE)

    def test_survives_renumbering(self):
        self.t.add_video(0, "Live Streamer CAP 4K", BY_ID)
        p = self.probe(["usb-AVerMedia_*-video-index0"])
        self.assertIs(p.poll(), Observation.IDLE)
        self.t.remove_all_video()
        self.assertIs(p.poll(), Observation.ABSENT)
        self.t.add_video(0, "Integrated Camera")
        node = self.t.add_video(2, "Live Streamer CAP 4K", BY_ID)
        self.t.add_process(4242, "zoom", node)
        self.assertEqual(p.resolve(), {str(node)})
        self.assertIs(p.poll(), Observation.IN_USE)

    def test_other_device_users_ignored(self):
        self.t.add_video(0, "Live Streamer CAP 4K", BY_ID)
        other = self.t.add_video(2, "Integrated Camera")
        self.t.add_process(4242, "cheese", other)
        self.assertIs(self.probe(["usb-AVerMedia_*-video-index0"]).poll(), Observation.IDLE)

    def test_ignore_processes(self):
        node = self.t.add_video(0, "Live Streamer CAP 4K", BY_ID)
        self.t.add_process(100, "obs", node)
        p = self.probe([BY_ID], ignore_processes=["obs"])
        self.assertIs(p.poll(), Observation.IDLE)
        self.t.add_process(101, "zoom", node)
        self.assertIs(p.poll(), Observation.IN_USE)

    def test_name_spec_for_loopback(self):
        node = self.t.add_video(10, "OBS Virtual Camera")
        self.t.add_process(100, "obs", node)
        p = self.probe(["name:obs virtual"], ignore_processes=["obs"])
        self.assertIs(p.poll(), Observation.IDLE)
        self.t.add_process(101, "zoom", node)
        self.assertIs(p.poll(), Observation.IN_USE)

    def test_virtual_camera_use_counts_but_not_presence(self):
        card = self.t.add_video(0, "Live Streamer CAP 4K", BY_ID)
        loop = self.t.add_video(10, "OBS Virtual Camera")
        self.t.add_process(100, "obs", card, loop)
        p = self.probe(["usb-AVerMedia_*-video-index0"], ignore_processes=["obs"],
                       virtual_specs=["name:OBS Virtual Camera"])
        self.assertIs(p.poll(), Observation.IDLE)
        self.t.add_process(101, "chrome", loop)
        self.assertIs(p.poll(), Observation.IN_USE)
        # KVM switch: card gone, loopback stays. Chrome still reading -> still a Session
        card.unlink()
        (self.t.dev / "v4l" / "by-id" / BY_ID).unlink()
        self.assertIs(p.poll(), Observation.IN_USE)
        # ...and once nobody reads it, the camera is ABSENT, not IDLE
        import shutil
        shutil.rmtree(self.t.proc / "101")
        self.assertIs(p.poll(), Observation.ABSENT)

    def test_absolute_path_spec(self):
        node = self.t.add_video(0, "X", BY_ID)
        link = self.t.dev / "v4l" / "by-id" / BY_ID
        self.t.add_process(5, "zoom", node)
        self.assertIs(self.probe([str(link)]).poll(), Observation.IN_USE)

    def test_any_camera(self):
        node = self.t.add_video(3, "Whatever")
        self.t.add_process(5, "zoom", node)
        self.assertIs(self.probe([], any_camera=True).poll(), Observation.IN_USE)

    def test_own_pid_excluded(self):
        node = self.t.add_video(0, "X", BY_ID)
        self.t.add_process(os.getpid(), "python3", node)
        self.assertIs(self.probe([BY_ID]).poll(), Observation.IDLE)


if __name__ == "__main__":
    unittest.main()
