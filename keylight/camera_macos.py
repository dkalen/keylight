"""macOS camera-in-use probe via CoreMediaIO, called through ctypes (no PyObjC).

Reads kCMIODevicePropertyDeviceIsRunningSomewhere for each video device. This
is the same signal that drives the green camera indicator, and it doesn't need
camera (TCC) permission because it never opens a stream.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import subprocess
from typing import Callable
from ctypes import POINTER, byref, c_char_p, c_int32, c_long, c_uint32, c_void_p

from .core import Observation


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


SYSTEM_OBJECT = 1
SCOPE_GLOBAL = _fourcc("glob")
ELEMENT_MAIN = 0
HW_DEVICES = _fourcc("dev#")
HW_RUNLOOP = _fourcc("rnlp")
PROP_NAME = _fourcc("lnam")
PROP_UID = _fourcc("uid ")
PROP_RUNNING_SOMEWHERE = _fourcc("gone")
CF_UTF8 = 0x08000100


class _Address(ctypes.Structure):
    _fields_ = [("mSelector", c_uint32), ("mScope", c_uint32), ("mElement", c_uint32)]


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
        self.cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFRelease.argtypes = [c_void_p]

        # Let CoreMediaIO run its own notification thread so the device list
        # tracks hot-plugging (KVM switches) without us running a CFRunLoop.
        null_runloop = c_void_p(None)
        self.cmio.CMIOObjectSetPropertyData(SYSTEM_OBJECT, byref(self._addr(HW_RUNLOOP)), 0, None,
                                            ctypes.sizeof(null_runloop), byref(null_runloop))

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

    def running_somewhere(self, obj: int) -> bool | None:
        value = c_uint32(0)
        used = c_uint32(0)
        if self.cmio.CMIOObjectGetPropertyData(obj, byref(self._addr(PROP_RUNNING_SOMEWHERE)), 0, None,
                                               ctypes.sizeof(value), byref(used), byref(value)) != 0:
            return None
        return bool(value.value)


def obs_process_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-x", "OBS"], capture_output=True, timeout=2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class MacCameraProbe:
    """CoreMediaIO can't say *which* process runs a device. So while OBS's own
    camera source is live, the physical camera's use is attributed to OBS and
    ignored (ADR 0001). OBS open with the camera source hidden, or with no camera
    source at all, doesn't hide anyone else's use. Without the websocket we can't
    tell, so a running OBS process is assumed to hold the camera."""

    def __init__(self, specs: list[str], any_camera: bool = False, virtual_specs: list[str] = (),
                 hub_holds_camera: Callable[[], bool | None] | None = None):
        self.specs = [s.lower() for s in specs]
        self.virtual_specs = [s.lower() for s in virtual_specs]
        self.any_camera = any_camera or not self.specs
        self.hub_holds_camera = hub_holds_camera
        self.api = _CMIO()

    def matching(self, specs: list[str] | None = None) -> list[tuple[int, str]]:
        any_cam = self.any_camera if specs is None else False
        specs = self.specs if specs is None else specs
        out = []
        for dev in self.api.devices():
            name, uid = self.api.string(dev, PROP_NAME), self.api.string(dev, PROP_UID)
            if any_cam or any(s in name.lower() or s in uid.lower() for s in specs):
                out.append((dev, name))
        return out

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
        if any(self.api.running_somewhere(d) for d, _ in physical) and not self._hub_up():
            return Observation.IN_USE
        return Observation.IDLE

    def describe(self) -> str:
        rows = [f"{name!r} running={self.api.running_somewhere(d)}"
                for d, name in self.matching() + (self.matching(self.virtual_specs) if self.virtual_specs else [])]
        return "devices=" + (", ".join(rows) if rows else "none") + f"  obs_holds_camera={self._hub_up()}"

    def all_devices(self) -> list[str]:
        return [f"{self.api.string(d, PROP_NAME)}  uid={self.api.string(d, PROP_UID)}" for d in self.api.devices()]
