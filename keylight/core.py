"""Shared core: config loading, the Key Light HTTP client, Kelvin/mired conversion.

Standard library only. Used by the CLI, the camera watcher and (later) the
auto-brightness loop.
"""

from __future__ import annotations

import enum
import json
import os
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

DEFAULT_PORT = 9123
MIRED_MIN, MIRED_MAX = 143, 344          # ~7000 K .. ~2900 K
KELVIN_MIN, KELVIN_MAX = 2900, 7000
BRIGHTNESS_MAX = 100
DEFAULT_MIN_BRIGHTNESS = 1               # API accepts 0-100 (fw 1.0.4); floor at 1 so "on" never means 0%


# --------------------------------------------------------------------------- errors

class KeylightError(Exception):
    """Base error. `exit_code` is what the CLI exits with."""
    exit_code = 1


class ConfigError(KeylightError):
    exit_code = 2


class UnreachableError(KeylightError):
    exit_code = 3


class UnsupportedDeviceError(KeylightError):
    exit_code = 4


# --------------------------------------------------------------------------- camera probes

class Observation(enum.Enum):
    """What a camera probe sees. Lives here, not in watch.py, so it stays a single
    class even when watch.py runs as __main__ (python -m keylight.watch)."""
    IN_USE = "in-use"
    IDLE = "idle"
    ABSENT = "absent"


# --------------------------------------------------------------------------- conversions

def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def kelvin_to_mired(kelvin: int) -> int:
    if kelvin <= 0:
        raise ValueError("kelvin must be positive")
    return clamp(round(1e6 / clamp(kelvin, KELVIN_MIN, KELVIN_MAX)), MIRED_MIN, MIRED_MAX)


def mired_to_kelvin(mired: int) -> int:
    return round(1e6 / mired) if mired > 0 else 0


# --------------------------------------------------------------------------- config

@dataclass
class Preset:
    brightness: int | None = None
    kelvin: int | None = None


@dataclass
class WatchConfig:
    # Physical camera. Linux: globs under /dev/v4l/by-id, absolute paths, or
    # "name:<substring>" matched against /sys/class/video4linux/*/name.
    # macOS: substrings of the CoreMediaIO device name or UID.
    linux_devices: list[str] = field(default_factory=list)
    macos_devices: list[str] = field(default_factory=list)
    # The Hub's virtual camera (same spec syntax). Use counts as a Session, but
    # its presence doesn't count as the camera being present (see absent_grace).
    linux_virtual_devices: list[str] = field(default_factory=list)
    macos_virtual_devices: list[str] = field(default_factory=list)
    any_camera: bool = False
    obs_sessions: bool = True            # OBS recording/streaming counts as a Session
    on_action: str = "auto"              # "auto" (Sun Schedule + pinned kelvin), "restore", or "preset:<name>"
    on_delay: float = 2.0
    off_delay: float = 5.0
    absent_grace: float = 15.0           # how long a vanished camera may stay gone and still count as the same Session
    poll_interval: float = 1.5           # only when the camera can't push changes (normally it does)
    ignore_processes: list[str] = field(default_factory=list)  # Linux: process names (comm) that don't count as camera users
    # macOS, while OBS's own camera source is live: the card in use counts as a
    # Session only if one of `mic_call_apps` is capturing a microphone (a call
    # next to OBS; calls keep the mic open while muted). Off: OBS holding the
    # camera hides every other use of it.
    mic_sessions: bool = True
    # Bundle-ID prefixes, so an app's helpers match too ("com.google.Chrome"
    # covers com.google.Chrome.helper, which is what captures for a tab).
    mic_call_apps: list[str] = field(default_factory=lambda: [
        "us.zoom.",                      # Zoom (us.zoom.xos, us.zoom.caphost)
        "com.microsoft.teams",           # Teams, classic and new (com.microsoft.teams2)
        "com.google.Chrome",             # Meet and other browser calls
        "com.apple.Safari", "com.apple.WebKit.GPU",   # Safari captures in WebKit's GPU process
        "org.mozilla.firefox",
        "com.microsoft.edgemac",
        "company.thebrowser.Browser",    # Arc
        "com.tinyspeck.slackmacgap",     # Slack huddles
        "com.apple.FaceTime", "com.apple.avconferenced",
        "com.cisco.webexmeetingsapp", "Cisco-Systems.Spark",   # Webex
    ])


@dataclass
class LocationConfig:
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class ObsConfig:
    host: str = "127.0.0.1"
    port: int = 4455
    password: str | None = None          # None: read OBS's own obs-websocket config on this machine
    password_file: str | None = None
    source: str | None = None            # None: the first video capture input in OBS
    timeout: float = 2.0


@dataclass
class AutoConfig:
    enabled: bool = True
    interval: float = 6.0
    target_luma: float = 0.42            # Key Side face luma, 0..1 sRGB-encoded
    deadband_stops: float = 0.15
    gain: float = 0.5
    max_step_stops: float = 0.33
    min_brightness: int = 5
    max_brightness: int = 80
    fade_ms: int = 3000
    manual_pause_min: float = 10.0
    ratio_mode: bool = True
    max_stops_under: float = 1.0         # face may be at most this far under the background
    background_roi: list[float] | None = None   # [x, y, w, h] normalized; None = auto (bright areas outside the face)
    key_side: str = "left"               # the subject's side the Light is on
    mirrored: bool = False               # true if the frame is mirrored (selfie view)
    day_brightness: int = 45             # Sun Schedule endpoints
    night_brightness: int = 20
    screenshot_width: int = 480
    min_face_score: float = 0.7
    model: str | None = None             # YuNet .onnx; None = installed default


DEFAULT_PRESETS: dict[str, Preset] = {}


@dataclass
class Config:
    host: str | None = None
    port: int = DEFAULT_PORT
    timeout: float = 1.5
    retries: int = 1
    mdns_fallback: bool = True
    min_brightness: int = DEFAULT_MIN_BRIGHTNESS
    kelvin: int | None = None            # pinned colour temperature (matches the camera's white balance)
    presets: dict[str, Preset] = field(default_factory=lambda: dict(DEFAULT_PRESETS))
    watch: WatchConfig = field(default_factory=WatchConfig)
    location: LocationConfig = field(default_factory=LocationConfig)
    obs: ObsConfig = field(default_factory=ObsConfig)
    auto: AutoConfig = field(default_factory=AutoConfig)
    raw: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None


def default_config_path() -> Path:
    env = os.environ.get("KEYLIGHT_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "keylight" / "config.toml"


def _typed(section: dict, key: str, types: type | tuple, where: str, default: Any) -> Any:
    if key not in section:
        return default
    value = section[key]
    # bool is an int subclass; don't let `true` pass as a number
    if isinstance(value, bool) and bool not in (types if isinstance(types, tuple) else (types,)):
        raise ConfigError(f"{where}.{key}: expected {types}, got bool")
    if not isinstance(value, types):
        raise ConfigError(f"{where}.{key}: expected {getattr(types, '__name__', types)}, got {type(value).__name__}")
    return value


def _str_list(section: dict, key: str, where: str) -> list[str]:
    value = _typed(section, key, list, where, [])
    if not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}.{key}: expected a list of strings")
    return list(value)


def _fill(obj: Any, section: dict, where: str) -> None:
    """Set dataclass fields from a TOML table, type-checked against the defaults."""
    for key, value in section.items():
        if not hasattr(obj, key):
            raise ConfigError(f"{where}.{key}: unknown key")
        current = getattr(obj, key)
        if isinstance(current, bool):
            setattr(obj, key, _typed(section, key, bool, where, current))
        elif isinstance(current, float):
            setattr(obj, key, float(_typed(section, key, (int, float), where, current)))
        elif isinstance(current, int):
            setattr(obj, key, _typed(section, key, int, where, current))
        elif isinstance(current, list) or key.endswith("_devices") or key == "ignore_processes":
            setattr(obj, key, _str_list(section, key, where))
        elif key == "background_roi":
            roi = _typed(section, key, list, where, None)
            if len(roi) != 4 or not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in roi):
                raise ConfigError(f"{where}.{key}: expected [x, y, w, h] with values in 0..1")
            setattr(obj, key, [float(v) for v in roi])
        elif key in ("latitude", "longitude"):
            setattr(obj, key, float(_typed(section, key, (int, float), where, None)))
        else:
            setattr(obj, key, _typed(section, key, str, where, current))


def parse_config(data: dict[str, Any], path: Path | None = None) -> Config:
    cfg = Config(raw=data, path=path)

    light = _typed(data, "light", dict, "config", {})
    cfg.host = _typed(light, "host", str, "light", None) or None
    cfg.port = _typed(light, "port", int, "light", DEFAULT_PORT)
    cfg.timeout = float(_typed(light, "timeout", (int, float), "light", cfg.timeout))
    cfg.retries = _typed(light, "retries", int, "light", cfg.retries)
    cfg.mdns_fallback = _typed(light, "mdns_fallback", bool, "light", cfg.mdns_fallback)
    cfg.min_brightness = clamp(_typed(light, "min_brightness", int, "light", cfg.min_brightness), 0, 100)
    cfg.kelvin = _typed(light, "kelvin", int, "light", None)

    presets = _typed(data, "presets", dict, "config", None)
    if presets is not None:
        cfg.presets = {}
        for name, p in presets.items():
            where = f"presets.{name}"
            if not isinstance(p, dict):
                raise ConfigError(f"{where}: expected a table")
            preset = Preset(
                brightness=_typed(p, "brightness", int, where, None),
                kelvin=_typed(p, "kelvin", int, where, None),
            )
            if preset.brightness is None and preset.kelvin is None:
                raise ConfigError(f"{where}: needs brightness and/or kelvin")
            cfg.presets[name] = preset

    for section, obj in (("watch", cfg.watch), ("location", cfg.location), ("obs", cfg.obs), ("auto", cfg.auto)):
        _fill(obj, _typed(data, section, dict, "config", {}), section)

    wc = cfg.watch
    if wc.on_action not in ("auto", "restore"):
        name = wc.on_action.removeprefix("preset:")
        if not wc.on_action.startswith("preset:") or name not in cfg.presets:
            raise ConfigError("watch.on_action: expected \"auto\", \"restore\" or \"preset:<name>\" "
                              f"naming a defined preset, got {wc.on_action!r}")
    if cfg.auto.key_side not in ("left", "right"):
        raise ConfigError("auto.key_side: expected \"left\" or \"right\"")
    loc = cfg.location
    if (loc.latitude is None) != (loc.longitude is None):
        raise ConfigError("location: set both latitude and longitude")
    return cfg


def _read_toml(p: Path) -> dict[str, Any]:
    try:
        with p.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: {e}") from e


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def local_config_path(config_path: Path) -> Path:
    """Per-machine overrides, never committed: $KEYLIGHT_LOCAL_CONFIG, else
    local.toml beside the config (beside the symlink, not the repo file)."""
    env = os.environ.get("KEYLIGHT_LOCAL_CONFIG")
    return Path(env).expanduser() if env else config_path.parent / "local.toml"


def _env_overrides() -> dict[str, Any]:
    loc: dict[str, float] = {}
    for key in ("latitude", "longitude"):
        raw = os.environ.get(f"KEYLIGHT_{key.upper()}")
        if raw:
            try:
                loc[key] = float(raw)
            except ValueError as e:
                raise ConfigError(f"KEYLIGHT_{key.upper()}: not a number: {raw!r}") from e
    return {"location": loc} if loc else {}


def load_config(path: str | Path | None = None) -> Config:
    """The shared config, then local.toml merged over it, then environment
    variables (KEYLIGHT_LATITUDE / KEYLIGHT_LONGITUDE)."""
    p = Path(path).expanduser() if path else default_config_path()
    if p.exists():
        data = _read_toml(p)
    elif path:   # explicitly requested file must exist
        raise ConfigError(f"config file not found: {p}")
    else:
        data = {}
    local = local_config_path(p)
    if local.exists():
        data = _merge(data, _read_toml(local))
    data = _merge(data, _env_overrides())
    return parse_config(data, p if p.exists() else None)


# --------------------------------------------------------------------------- client

@dataclass
class LightState:
    on: bool
    brightness: int
    mired: int

    @property
    def kelvin(self) -> int:
        return mired_to_kelvin(self.mired)

    def to_dict(self) -> dict[str, Any]:
        return {"on": self.on, "brightness": self.brightness, "kelvin": self.kelvin, "mired": self.mired}


# No proxies for a LAN device, and no Origin header ever (patched firmware,
# CVE-2025-7202, rejects requests that carry one). urllib never adds Origin.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class KeyLight:
    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 1.5,
                 retries: int = 1, min_brightness: int = DEFAULT_MIN_BRIGHTNESS):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = max(0, retries)
        self.min_brightness = min_brightness

    @classmethod
    def from_config(cls, cfg: Config, host: str | None = None) -> "KeyLight":
        host = host or cfg.host
        if not host:
            raise ConfigError("no light host configured (set [light] host in config, or pass --host)")
        return cls(host, cfg.port, cfg.timeout, cfg.retries, cfg.min_brightness)

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        last_error: Exception | None = None
        for _ in range(self.retries + 1):
            req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
            try:
                with _opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                break
            except urllib.error.HTTPError as e:
                detail = e.read()[:200].decode(errors="replace").strip()
                raise UnsupportedDeviceError(
                    f"{method} {path} on {self.host}: HTTP {e.code} {e.reason}" + (f" ({detail})" if detail else "")
                ) from e
            except (urllib.error.URLError, OSError) as e:
                last_error = e
        else:
            reason = getattr(last_error, "reason", last_error)
            raise UnreachableError(f"light at {self.host}:{self.port} is unreachable: {reason}")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as e:
            raise UnsupportedDeviceError(f"{method} {path} on {self.host}: response is not JSON") from e

    @staticmethod
    def _parse_lights(payload: Any) -> LightState:
        try:
            light = payload["lights"][0]
            return LightState(on=bool(light["on"]), brightness=int(light["brightness"]),
                              mired=int(light["temperature"]))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise UnsupportedDeviceError(f"unexpected /elgato/lights payload: {payload!r}") from e

    def get_state(self) -> LightState:
        return self._parse_lights(self._request("GET", "/elgato/lights"))

    def info(self) -> dict[str, Any]:
        payload = self._request("GET", "/elgato/accessory-info")
        if not isinstance(payload, dict):
            raise UnsupportedDeviceError(f"unexpected /elgato/accessory-info payload: {payload!r}")
        return payload

    def clamp_brightness(self, value: int) -> int:
        return clamp(value, max(1, self.min_brightness), BRIGHTNESS_MAX)

    def set_state(self, on: bool | None = None, brightness: int | None = None,
                  mired: int | None = None) -> LightState:
        light: dict[str, int] = {}
        if on is not None:
            light["on"] = int(on)
        if brightness is not None:
            light["brightness"] = self.clamp_brightness(brightness)
        if mired is not None:
            light["temperature"] = clamp(mired, MIRED_MIN, MIRED_MAX)
        if not light:
            return self.get_state()
        payload = self._request("PUT", "/elgato/lights", {"numberOfLights": 1, "lights": [light]})
        try:
            return self._parse_lights(payload)
        except UnsupportedDeviceError:
            return self.get_state()   # some firmwares return an empty body

    def fade_to(self, brightness: int | None = None, mired: int | None = None,
                duration_ms: int = 0, on: bool | None = None,
                sleep: Callable[[float], None] = time.sleep, step_ms: int = 80) -> LightState:
        """Move to a target with stepped PUTs. Only fades while the light is on;
        otherwise (or with duration 0) it is a single PUT."""
        if brightness is not None:
            brightness = self.clamp_brightness(brightness)
        if mired is not None:
            mired = clamp(mired, MIRED_MIN, MIRED_MAX)
        cur = self.get_state() if duration_ms > 0 else None
        if cur is None or not cur.on:
            return self.set_state(on=on, brightness=brightness, mired=mired)

        steps = clamp(duration_ms // step_ms, 1, 40)
        b0, m0 = cur.brightness, cur.mired
        last: tuple[int | None, int | None] = (b0, m0)
        state = cur
        for i in range(1, steps + 1):
            t = i / steps
            b = round(b0 + (brightness - b0) * t) if brightness is not None else None
            m = round(m0 + (mired - m0) * t) if mired is not None else None
            if (b, m) != last:
                state = self.set_state(brightness=b, mired=m)
                last = (b, m)
            if i < steps:
                sleep(duration_ms / steps / 1000)
        if on is not None and on != state.on:
            state = self.set_state(on=on)
        return state

    def apply_preset(self, preset: Preset, duration_ms: int = 0) -> LightState:
        mired = kelvin_to_mired(preset.kelvin) if preset.kelvin is not None else None
        return self.fade_to(brightness=preset.brightness, mired=mired, duration_ms=duration_ms, on=True)


T = TypeVar("T")


def run_with_fallback(cfg: Config, fn: Callable[[KeyLight], T], host: str | None = None,
                      warn: Callable[[str], None] | None = None) -> T:
    """Run fn against the configured light; if it's unreachable (or unset) and
    mdns_fallback is on, discover a light over mDNS and retry once."""
    from . import mdns

    target = host or cfg.host
    if target:
        try:
            return fn(KeyLight.from_config(cfg, target))
        except UnreachableError:
            if not cfg.mdns_fallback or host:
                raise
            first_error = True
    else:
        if not cfg.mdns_fallback:
            raise ConfigError("no light host configured and mdns_fallback is disabled")
        first_error = False

    found = mdns.find_light()      # raises UnsupportedDeviceError for tls= records
    if found is None:
        if first_error:
            raise UnreachableError(f"light at {target}:{cfg.port} is unreachable and none was found via mDNS")
        raise ConfigError("no light host configured and none was found via mDNS (_elg._tcp)")
    if warn:
        warn(f"using {found.address}:{found.port} found via mDNS" +
             (f" (configured {target} is unreachable; update [light] host)" if target else ""))
    client = KeyLight(found.address, found.port, cfg.timeout, cfg.retries, cfg.min_brightness)
    return fn(client)
