"""macOS camera-in-use probe via CoreMediaIO, called through ctypes (no PyObjC).

Reads kCMIODevicePropertyDeviceIsRunningSomewhere for each video device. This
is the same signal that drives the green camera indicator, and it doesn't need
camera (TCC) permission because it never opens a stream. `listen()` also asks
CoreMediaIO to report changes to it, and to the device list, as they happen.

CoreMediaIO delivers those reports, and refreshes its device list, only through
the main thread's CFRunLoop (setting kCMIOHardwarePropertyRunLoop to NULL or to
another thread's loop didn't change that on macOS 26). Without a running loop a
long-lived process never sees a camera arrive, so `keylight-watch` runs
`run_main_loop()` on the main thread and its watch loop on a worker.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import subprocess
import threading
from typing import Callable
from ctypes import POINTER, byref, c_char_p, c_int32, c_long, c_uint32, c_void_p

from .core import Observation


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


SYSTEM_OBJECT = 1
SCOPE_GLOBAL = _fourcc("glob")
ELEMENT_MAIN = 0
HW_DEVICES = _fourcc("dev#")
PROP_NAME = _fourcc("lnam")
PROP_UID = _fourcc("uid ")
PROP_RUNNING_SOMEWHERE = _fourcc("gone")
CF_UTF8 = 0x08000100


class _Address(ctypes.Structure):
    _fields_ = [("mSelector", c_uint32), ("mScope", c_uint32), ("mElement", c_uint32)]


# OSStatus (*)(CMIOObjectID, UInt32 numberAddresses, const CMIOObjectPropertyAddress[], void *clientData)
_LISTENER = ctypes.CFUNCTYPE(c_int32, c_uint32, c_uint32, POINTER(_Address), c_void_p)


class _CMIO:
    def __init__(self) -> None:
        cf_path = ctypes.util.find_library("CoreFoundation")
        cmio_path = ctypes.util.find_library("CoreMediaIO")
        if not cf_path or not cmio_path:
            raise OSError("CoreMediaIO/CoreFoundation not found (not macOS?)")
        self.cf = ctypes.CDLL(cf_path)
        self.cmio = ctypes.CDLL(cmio_path)
        self.cmio.CMIOObjectGetPropertyDataSize.argtypes = [
            c_uint32, POINTER(_Address), c_uint32, c_void_p, POINTER(c_uint32)]
        self.cmio.CMIOObjectGetPropertyDataSize.restype = c_int32
        self.cmio.CMIOObjectGetPropertyData.argtypes = [
            c_uint32, POINTER(_Address), c_uint32, c_void_p, c_uint32, POINTER(c_uint32), c_void_p]
        self.cmio.CMIOObjectGetPropertyData.restype = c_int32
        self.cmio.CMIOObjectSetPropertyData.argtypes = [
            c_uint32, POINTER(_Address), c_uint32, c_void_p, c_uint32, c_void_p]
        self.cmio.CMIOObjectSetPropertyData.restype = c_int32
        self.cmio.CMIOObjectAddPropertyListener.argtypes = [c_uint32, POINTER(_Address), _LISTENER, c_void_p]
        self.cmio.CMIOObjectAddPropertyListener.restype = c_int32
        self._listeners: list = []    # ctypes callbacks must stay alive while registered
        self.cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFRelease.argtypes = [c_void_p]

    @staticmethod
    def _addr(selector: int) -> _Address:
        return _Address(selector, SCOPE_GLOBAL, ELEMENT_MAIN)

    def devices(self) -> list[int]:
        addr = self._addr(HW_DEVICES)
        size = c_uint32(0)
        if self.cmio.CMIOObjectGetPropertyDataSize(SYSTEM_OBJECT, byref(addr), 0, None, byref(size)) != 0:
            return []
        count = size.value // ctypes.sizeof(c_uint32)
        if count == 0:
            return []
        ids = (c_uint32 * count)()
        used = c_uint32(0)
        if self.cmio.CMIOObjectGetPropertyData(SYSTEM_OBJECT, byref(addr), 0, None, size, byref(used), ids) != 0:
            return []
        return list(ids[: used.value // ctypes.sizeof(c_uint32)])

    def string(self, obj: int, selector: int) -> str:
        ref = c_void_p()
        used = c_uint32(0)
        if self.cmio.CMIOObjectGetPropertyData(obj, byref(self._addr(selector)), 0, None,
                                               ctypes.sizeof(ref), byref(used), byref(ref)) != 0 or not ref:
            return ""
        try:
            buf = ctypes.create_string_buffer(1024)
            return buf.value.decode() if self.cf.CFStringGetCString(ref, buf, len(buf), CF_UTF8) else ""
        finally:
            self.cf.CFRelease(ref)

    def add_listener(self, obj: int, selector: int, fn: Callable[[], None]) -> bool:
        """Call fn (on CoreMediaIO's notification thread) whenever the property changes."""
        def proc(_obj, _count, _addrs, _data):
            try:
                fn()
            except Exception:    # an exception can't cross back into CoreMediaIO
                pass
            return 0
        callback = _LISTENER(proc)
        if self.cmio.CMIOObjectAddPropertyListener(obj, byref(self._addr(selector)), callback, None) != 0:
            return False
        self._listeners.append(callback)
        return True

    def running_somewhere(self, obj: int) -> bool | None:
        value = c_uint32(0)
        used = c_uint32(0)
        if self.cmio.CMIOObjectGetPropertyData(obj, byref(self._addr(PROP_RUNNING_SOMEWHERE)), 0, None,
                                               ctypes.sizeof(value), byref(used), byref(value)) != 0:
            return None
        return bool(value.value)


def run_main_loop(stop: threading.Event, slice_s: float = 0.5) -> None:
    """Run the main thread's CFRunLoop until `stop` is set. Must be called on the
    main thread; CoreMediaIO's listener callbacks run inside it."""
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    cf.CFRunLoopRunInMode.argtypes = [c_void_p, ctypes.c_double, ctypes.c_bool]
    cf.CFRunLoopRunInMode.restype = c_int32
    mode = c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
    while not stop.is_set():
        if cf.CFRunLoopRunInMode(mode, slice_s, False) == 1:   # kCFRunLoopRunFinished: no sources yet
            stop.wait(slice_s)


def obs_process_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-x", "OBS"], capture_output=True, timeout=2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class MacCameraProbe:
    """CoreMediaIO can't say *which* process runs a device. So while OBS's own
    camera source is live, the physical camera's use is attributed to OBS and
    ignored (ADR 0001), unless `mic_users` reports a call app capturing a
    microphone: then it's a call next to OBS. OBS open with the camera source
    hidden, or with no camera source at all, doesn't hide anyone else's use.
    Without the websocket we can't tell, so a running OBS process is assumed to
    hold the camera."""
    MIC_POLL = 0.3   # ~4 ms per check (MicUsers), so ~1.5% of a core, and only while OBS holds the card

    def __init__(self, specs: list[str], any_camera: bool = False, virtual_specs: list[str] = (),
                 hub_holds_camera: Callable[[], bool | None] | None = None,
                 mic_users: Callable[[], list[str]] | None = None):
        self.specs = [s.lower() for s in specs]
        self.virtual_specs = [s.lower() for s in virtual_specs]
        self.any_camera = any_camera or not self.specs
        self.hub_holds_camera = hub_holds_camera
        self.mic_users = mic_users
        self._mic_watch = False      # last poll: card in use but attributed to OBS
        self._mic_call = False       # last poll: IN_USE because a call app has the mic
        self.api = _CMIO()
        self._on_change: Callable[[], None] | None = None
        self._watched: set[int] = set()
        self._lock = threading.Lock()

    def matching(self, specs: list[str] | None = None) -> list[tuple[int, str]]:
        any_cam = self.any_camera if specs is None else False
        specs = self.specs if specs is None else specs
        out = []
        for dev in self.api.devices():
            name, uid = self.api.string(dev, PROP_NAME), self.api.string(dev, PROP_UID)
            if any_cam or any(s in name.lower() or s in uid.lower() for s in specs):
                out.append((dev, name))
        return out

    def listen(self, on_change: Callable[[], None]) -> bool:
        """Call on_change whenever any camera starts or stops running, or cameras
        come and go (KVM switch). It runs on CoreMediaIO's thread, so it should
        only wake the watcher. Returns False if CoreMediaIO refused a listener;
        polling still covers that case."""
        self._on_change = on_change

        def devices_changed() -> None:
            self._watch_devices()
            on_change()

        return self.api.add_listener(SYSTEM_OBJECT, HW_DEVICES, devices_changed) & self._watch_devices()

    def _watch_devices(self) -> bool:
        """Put a running-state listener on every device that doesn't have one yet."""
        ok = True
        with self._lock:
            current = set(self.api.devices())
            self._watched &= current          # a removed device's listener goes with it
            for dev in current - self._watched:
                if self.api.add_listener(dev, PROP_RUNNING_SOMEWHERE, self._on_change):
                    self._watched.add(dev)
                else:
                    ok = False
        return ok

    def _hub_up(self) -> bool:
        """Is OBS (rather than someone else) what's running the camera?"""
        answer = self.hub_holds_camera() if self.hub_holds_camera else None
        if answer is not None:
            return answer
        return obs_process_running()

    def poll(self) -> Observation:
        virtual = self.matching(self.virtual_specs) if self.virtual_specs else []
        if any(self.api.running_somewhere(d) for d, _ in virtual):
            return Observation.IN_USE
        physical = [(d, n) for d, n in self.matching() if (d, n) not in virtual]
        if not physical:
            return Observation.ABSENT
        self._mic_watch = self._mic_call = False
        if any(self.api.running_somewhere(d) for d, _ in physical):
            if not self._hub_up():
                return Observation.IN_USE
            self._mic_watch = self.mic_users is not None
            if self._mic_in_use():
                self._mic_call = True
                return Observation.IN_USE
        return Observation.IDLE

    def on_delay(self) -> float | None:
        """0 when the last IN_USE came from a call app's mic next to OBS: on_delay
        debounces brief camera opens, but here the camera is OBS's and already
        running, and a call app has the mic, so there's nothing to debounce.
        None (use watch.on_delay) otherwise."""
        return 0.0 if self._mic_call else None

    def next_wake(self, now: float) -> float | None:
        """While OBS holds the card, re-check the microphone every MIC_POLL s.
        CoreAudio accepts a listener on kAudioProcessPropertyIsRunningInput but
        never fires it (macOS 26), and the input device's own running flag stays
        set while OBS has the mic open, so a call joining or leaving next to OBS
        pushes nothing. Nothing is polled otherwise."""
        return self.MIC_POLL if self._mic_watch else None

    def _mic_in_use(self) -> bool:
        """Is a call app capturing a microphone (a call next to OBS)?"""
        return bool(self.mic_users and self.mic_users())

    def describe(self) -> str:
        rows = [f"{name!r} running={self.api.running_somewhere(d)}"
                for d, name in self.matching() + (self.matching(self.virtual_specs) if self.virtual_specs else [])]
        out = "devices=" + (", ".join(rows) if rows else "none") + f"  obs_holds_camera={self._hub_up()}"
        if self.mic_users is not None:
            calls = self.mic_users()
            others = [b for b in getattr(self.mic_users, "capturing", lambda: calls)() if b not in calls]
            out += f"  mic_call_apps={', '.join(calls) or 'none'}"
            if others:
                out += f" (also capturing, not counted: {', '.join(others)})"
        return out

    def all_devices(self) -> list[str]:
        return [f"{self.api.string(d, PROP_NAME)}  uid={self.api.string(d, PROP_UID)}" for d in self.api.devices()]
