"""Linux camera-in-use probe: is any process holding the watched V4L2 node open?

Devices are re-resolved on every poll, so /dev/videoN renumbering after a KVM
switch doesn't matter. Specs in config can be:
  - a glob under /dev/v4l/by-id (e.g. "usb-AVerMedia_Live_Streamer_CAP_4K_*-video-index0")
  - an absolute path (symlinks are resolved)
  - "name:<substring>" matched case-insensitively against /sys/class/video4linux/*/name
    (useful for v4l2loopback devices like "OBS Virtual Camera", which have no by-id link)
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .core import Observation


class LinuxCameraProbe:
    def __init__(self, specs: list[str], any_camera: bool = False, ignore_processes: list[str] = (),
                 dev_root: str = "/dev", proc_root: str = "/proc", sys_root: str = "/sys",
                 virtual_specs: list[str] = ()):
        self.specs = list(specs)
        self.virtual_specs = list(virtual_specs)
        self.any_camera = any_camera or not self.specs
        self.ignore = set(ignore_processes)
        self.dev = Path(dev_root)
        self.proc = Path(proc_root)
        self.sysfs = Path(sys_root) / "class" / "video4linux"
        self.uid = os.getuid()
        self.pid = os.getpid()

    def _by_name(self) -> dict[str, str]:
        """videoN -> device name from sysfs."""
        names = {}
        try:
            for entry in os.scandir(self.sysfs):
                try:
                    names[entry.name] = (Path(entry.path) / "name").read_text().strip()
                except OSError:
                    pass
        except OSError:
            pass
        return names

    def resolve(self, specs: list[str] | None = None) -> set[str]:
        """Real device paths currently matching `specs` (default: the physical camera)."""
        if specs is None:
            if self.any_camera:
                return {str(self.dev / n) for n in self._by_name()}
            specs = self.specs
        found: set[str] = set()
        by_id = self.dev / "v4l" / "by-id"
        names: dict[str, str] | None = None
        for spec in specs:
            if spec.startswith("name:"):
                if names is None:
                    names = self._by_name()
                needle = spec[5:].lower()
                found.update(str(self.dev / n) for n, label in names.items() if needle in label.lower())
            elif spec.startswith("/"):
                if os.path.exists(spec):
                    found.add(os.path.realpath(spec))
            else:
                try:
                    for entry in os.scandir(by_id):
                        if fnmatch.fnmatch(entry.name, spec) and os.path.exists(entry.path):
                            found.add(os.path.realpath(entry.path))
                except OSError:
                    pass
        return found

    def users(self, devices: set[str]) -> list[tuple[int, str]]:
        """(pid, comm) of our-user processes holding any of `devices` open, minus ignored names."""
        result = []
        try:
            procs = os.scandir(self.proc)
        except OSError:
            return result
        with procs:
            for entry in procs:
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                if pid == self.pid:
                    continue
                try:
                    if entry.stat().st_uid != self.uid:
                        continue        # can't read other users' fds anyway
                    fds = os.scandir(os.path.join(entry.path, "fd"))
                except OSError:
                    continue
                with fds:
                    for fd in fds:
                        try:
                            target = os.readlink(fd.path)
                        except OSError:
                            continue
                        if target in devices:
                            try:
                                comm = Path(entry.path, "comm").read_text().strip()
                            except OSError:
                                comm = "?"
                            if comm not in self.ignore:
                                result.append((pid, comm))
                            break
        return result

    def poll(self) -> Observation:
        """IN_USE if anyone (not ignored) holds the camera or the virtual camera;
        ABSENT only depends on the physical camera, so an always-present
        v4l2loopback device doesn't hide a KVM switch."""
        physical = self.resolve()
        devices = physical | (self.resolve(self.virtual_specs) if self.virtual_specs else set())
        if devices and self.users(devices):
            return Observation.IN_USE
        return Observation.IDLE if physical else Observation.ABSENT

    def all_devices(self) -> list[str]:
        names = self._by_name()
        lines = [f"{self.dev / n}  {names[n]}" for n in sorted(names)]
        try:
            by_id = self.dev / "v4l" / "by-id"
            lines += [f"{by_id / e} -> {os.path.realpath(by_id / e)}" for e in sorted(os.listdir(by_id))]
        except OSError:
            pass
        return lines

    def describe(self) -> str:
        devices = sorted(self.resolve() | (self.resolve(self.virtual_specs) if self.virtual_specs else set()))
        users = self.users(set(devices)) if devices else []
        return f"devices={devices or 'none'} users={users or 'none'}"
