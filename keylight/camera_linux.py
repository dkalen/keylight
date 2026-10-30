"""Linux camera-in-use probe: is any process holding the watched V4L2 node open?

Devices are re-resolved on every poll, so /dev/videoN renumbering after a KVM
switch doesn't matter. Specs in config can be:
  - a glob under /dev/v4l/by-id (e.g. "usb-AVerMedia_Live_Streamer_CAP_4K_*-video-index0")
  - an absolute path (symlinks are resolved)
  - "name:<substring>" matched case-insensitively against /sys/class/video4linux/*/name
    (useful for v4l2loopback devices like "OBS Virtual Camera", which have no by-id link)

`listen()` pushes changes with inotify: an open or close of any /dev/video*
node, and video nodes or their by-id links appearing, disappearing or changing
permissions. inotify can't say who opened a node, so each event only wakes the
watcher, which then scans /proc as usual. This process never opens a camera, so
its own scans can't trigger events.
"""

from __future__ import annotations

import ctypes
import fnmatch
import logging
import os
import select
import struct
import threading
from pathlib import Path
from typing import Callable

from .core import Observation

log = logging.getLogger("keylight-watch")

IN_ATTRIB, IN_CLOSE_WRITE, IN_CLOSE_NOWRITE, IN_OPEN = 0x4, 0x8, 0x10, 0x20
IN_MOVED_FROM, IN_MOVED_TO, IN_CREATE, IN_DELETE, IN_DELETE_SELF = 0x40, 0x80, 0x100, 0x200, 0x400
IN_Q_OVERFLOW, IN_IGNORED = 0x4000, 0x8000
NODE_MASK = IN_OPEN | IN_CLOSE_WRITE | IN_CLOSE_NOWRITE
DIR_MASK = IN_CREATE | IN_DELETE | IN_ATTRIB | IN_MOVED_FROM | IN_MOVED_TO | IN_DELETE_SELF
_EVENT = struct.Struct("iIII")      # wd, mask, cookie, len (then the name)


class _Inotify:
    def __init__(self) -> None:
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.fd = self.libc.inotify_init1(os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._wake_r, self._wake_w = os.pipe()     # stop() wakes a blocked read()

    def add(self, path: str, mask: int) -> int | None:
        """Watch descriptor, or None if the path can't be watched (missing, no access).
        Watching an already-watched path returns the same descriptor."""
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), mask)
        return wd if wd >= 0 else None

    def stop(self) -> None:
        os.write(self._wake_w, b"x")

    def close(self) -> None:
        """Only from the thread that calls read(), so a recycled fd number can't be read by mistake."""
        for fd in (self.fd, self._wake_r, self._wake_w):
            os.close(fd)

    def read(self) -> list[tuple[int, int, str]] | None:
        """Block until events arrive; (wd, mask, name) for each, or None once stop() was called."""
        ready, _, _ = select.select([self.fd, self._wake_r], [], [])
        if self._wake_r in ready:
            return None
        buf = os.read(self.fd, 64 * 1024)
        out, i = [], 0
        while i + _EVENT.size <= len(buf):
            wd, mask, _cookie, n = _EVENT.unpack_from(buf, i)
            name = buf[i + _EVENT.size:i + _EVENT.size + n].rstrip(b"\0").decode(errors="replace")
            out.append((wd, mask, name))
            i += _EVENT.size + n
        return out


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
        self._ino: _Inotify | None = None
        self._dirs: dict[int, str] = {}      # watch descriptor -> watched directory
        self._on_change: Callable[[], None] | None = None

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

    def listen(self, on_change: Callable[[], None]) -> bool:
        """Call on_change (on a reader thread) whenever a video node is opened or
        closed, or video nodes come and go. False if inotify isn't available."""
        try:
            self._ino = _Inotify()
        except (OSError, AttributeError) as e:
            log.info("inotify unavailable: %s", e)
            return False
        self._on_change = on_change
        if not self._rewatch():
            return False
        threading.Thread(target=self._read_loop, name="inotify", daemon=True).start()
        return True

    def _rewatch(self) -> bool:
        """(Re)add watches on /dev, /dev/v4l, /dev/v4l/by-id and every video node.
        Idempotent. False if /dev itself can't be watched."""
        dirs = {}
        for d in (self.dev, self.dev / "v4l", self.dev / "v4l" / "by-id"):
            wd = self._ino.add(str(d), DIR_MASK)
            if wd is not None:
                dirs[wd] = str(d)
        self._dirs = dirs
        try:
            nodes = [e.path for e in os.scandir(self.dev) if e.name.startswith("video")]
        except OSError:
            nodes = []
        for node in nodes:
            self._ino.add(node, NODE_MASK)    # no access yet (udev ACL pending): IN_ATTRIB retries
        return str(self.dev) in dirs.values()

    def stop_listening(self) -> None:
        if self._ino is not None:
            self._ino.stop()

    def _read_loop(self) -> None:
        ino = self._ino
        try:
            self._dispatch(ino)
        finally:
            ino.close()

    def _dispatch(self, ino: _Inotify) -> None:
        while True:
            try:
                events = ino.read()
            except OSError as e:
                log.warning("inotify stopped: %s; camera changes won't be noticed until restart", e)
                return
            if events is None:
                return
            relevant = rewatch = False
            for wd, mask, name in events:
                directory = self._dirs.get(wd)
                if mask & IN_Q_OVERFLOW:
                    relevant = rewatch = True
                elif mask & IN_IGNORED:
                    rewatch = rewatch or directory is not None    # a watched directory went away
                elif directory == str(self.dev):
                    if name.startswith(("video", "v4l")):
                        relevant = rewatch = True
                elif directory is not None:                    # /dev/v4l or by-id changed
                    relevant = rewatch = True
                elif mask & NODE_MASK:
                    relevant = True
            if rewatch:
                self._rewatch()
            if relevant:
                try:
                    self._on_change()
                except Exception:
                    log.exception("camera change handler failed")

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
