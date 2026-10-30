"""Which apps are recording from a microphone right now (macOS 14+), via CoreAudio's
process objects, called through ctypes (no PyObjC).

Unlike CoreMediaIO for cameras, CoreAudio can say *who* is capturing input
(kAudioProcessPropertyIsRunningInput per process). That's how a call is told
apart from OBS while OBS's own camera source is live (ADR 0001): only listed call
apps count, so OBS's own mic, dictation or a meeting recorder don't. It only reads
flags and bundle IDs, never audio, so it needs no microphone (TCC) permission.

It can't notify: a listener on kAudioProcessPropertyIsRunningInput registers but
never fires (macOS 26), so the camera probe re-reads this every MIC_POLL s, and
only while OBS holds the card.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import POINTER, byref, c_char_p, c_int32, c_long, c_uint32, c_void_p


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


SYSTEM_OBJECT = 1
SCOPE_GLOBAL = _fourcc("glob")
ELEMENT_MAIN = 0
PROCESS_LIST = _fourcc("prs#")         # kAudioHardwarePropertyProcessObjectList
PROCESS_BUNDLE_ID = _fourcc("pbid")    # kAudioProcessPropertyBundleID
PROCESS_RUNNING_INPUT = _fourcc("piri")  # kAudioProcessPropertyIsRunningInput
CF_UTF8 = 0x08000100


class _Address(ctypes.Structure):
    _fields_ = [("mSelector", c_uint32), ("mScope", c_uint32), ("mElement", c_uint32)]


class _CoreAudio:
    def __init__(self) -> None:
        ca_path = ctypes.util.find_library("CoreAudio")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not ca_path or not cf_path:
            raise OSError("CoreAudio/CoreFoundation not found (not macOS?)")
        self.ca = ctypes.CDLL(ca_path)
        self.cf = ctypes.CDLL(cf_path)
        self.ca.AudioObjectGetPropertyDataSize.argtypes = [
            c_uint32, POINTER(_Address), c_uint32, c_void_p, POINTER(c_uint32)]
        self.ca.AudioObjectGetPropertyDataSize.restype = c_int32
        self.ca.AudioObjectGetPropertyData.argtypes = [
            c_uint32, POINTER(_Address), c_uint32, c_void_p, POINTER(c_uint32), c_void_p]
        self.ca.AudioObjectGetPropertyData.restype = c_int32
        self.cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFRelease.argtypes = [c_void_p]

    @staticmethod
    def _addr(selector: int) -> _Address:
        return _Address(selector, SCOPE_GLOBAL, ELEMENT_MAIN)

    def processes(self) -> list[int]:
        addr = self._addr(PROCESS_LIST)
        size = c_uint32(0)
        if self.ca.AudioObjectGetPropertyDataSize(SYSTEM_OBJECT, byref(addr), 0, None, byref(size)) != 0:
            return []
        count = size.value // ctypes.sizeof(c_uint32)
        if count == 0:
            return []
        ids = (c_uint32 * count)()
        if self.ca.AudioObjectGetPropertyData(SYSTEM_OBJECT, byref(addr), 0, None, byref(size), ids) != 0:
            return []
        return list(ids[: size.value // ctypes.sizeof(c_uint32)])

    def running_input(self, obj: int) -> bool:
        value = c_uint32(0)
        size = c_uint32(ctypes.sizeof(value))
        if self.ca.AudioObjectGetPropertyData(obj, byref(self._addr(PROCESS_RUNNING_INPUT)), 0, None,
                                              byref(size), byref(value)) != 0:
            return False
        return bool(value.value)

    def bundle_id(self, obj: int) -> str:
        ref = c_void_p()
        size = c_uint32(ctypes.sizeof(ref))
        if self.ca.AudioObjectGetPropertyData(obj, byref(self._addr(PROCESS_BUNDLE_ID)), 0, None,
                                              byref(size), byref(ref)) != 0 or not ref:
            return ""
        try:
            buf = ctypes.create_string_buffer(512)
            return buf.value.decode() if self.cf.CFStringGetCString(ref, buf, len(buf), CF_UTF8) else ""
        finally:
            self.cf.CFRelease(ref)


class MicUsers:
    """Bundle IDs of the call apps capturing audio input (watch.mic_call_apps,
    matched as prefixes so an app's helper processes count too). `capturing()`
    lists every app capturing, call app or not, for `--probe`.

    Reading a process's input flag costs ~0.23 ms, so only call-app processes are
    read on each check; bundle IDs are cached per process object and looked up
    only for new ones. A check is ~4 ms instead of ~11 ms for all ~45 processes."""

    def __init__(self, call_apps: list[str]):
        self.call_apps = tuple(a.lower() for a in call_apps)
        self.api = _CoreAudio()
        self._bundles: dict[int, str] = {}

    def _bundle(self, proc: int) -> str:
        bid = self._bundles.get(proc)
        if bid is None:
            bid = self._bundles[proc] = self.api.bundle_id(proc) or f"process object {proc}"
        return bid

    def capturing(self) -> list[str]:
        return [self._bundle(proc) for proc in self.api.processes() if self.api.running_input(proc)]

    def __call__(self) -> list[str]:
        procs = self.api.processes()
        live = set(procs)
        for gone in [p for p in self._bundles if p not in live]:
            del self._bundles[gone]        # object IDs aren't reused, but don't grow forever
        calls = [p for p in procs if self._bundle(p).lower().startswith(self.call_apps)]
        return [self._bundle(p) for p in calls if self.api.running_input(p)]
