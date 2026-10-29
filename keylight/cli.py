"""`keylight` command-line interface.

Exit codes: 0 ok, 1 other error, 2 usage/config error, 3 light unreachable,
4 unsupported device or unexpected response.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, mdns
from .core import (BRIGHTNESS_MAX, Config, KeyLight, KeylightError, LightState, kelvin_to_mired,
                   load_config, run_with_fallback)


def describe(state: LightState) -> str:
    power = "on" if state.on else "off"
    return f"{power}  brightness {state.brightness}%  {state.kelvin} K ({state.mired} mired)"


def parse_brightness(text: str) -> tuple[str, int]:
    """'40' -> ('abs', 40); '+10' -> ('rel', 10); '-5' -> ('rel', -5)."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected 0-100, +N or -N, got {text!r}")
    if text.startswith(("+", "-")):
        return "rel", value
    if not 0 <= value <= BRIGHTNESS_MAX:
        raise argparse.ArgumentTypeError(f"brightness must be 0-100, got {value}")
    return "abs", value


def positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="keylight", description="Control an Elgato Key Light over the local HTTP API.")
    p.add_argument("--config", help="config file (default ~/.config/keylight/config.toml)")
    p.add_argument("--host", help="light IP/hostname, overrides config")
    p.add_argument("-q", "--quiet", action="store_true", help="print nothing on success")
    p.add_argument("--version", action="version", version=f"keylight {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def fade(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--fade-ms", type=int, default=0, metavar="MS", help="fade over MS milliseconds (client-side)")

    sub.add_parser("on", help="turn the light on")
    sub.add_parser("off", help="turn the light off")
    sub.add_parser("toggle", help="toggle on/off")
    sp = sub.add_parser("status", help="show current state")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("brightness", help="set brightness: 0-100 (0 = off), +N or -N")
    sp.add_argument("value", type=parse_brightness)
    fade(sp)
    sp = sub.add_parser("temp", help="set colour temperature in Kelvin (2900-7000)")
    sp.add_argument("kelvin", type=positive_int)
    fade(sp)
    sp = sub.add_parser("set", help="set brightness and/or temperature")
    sp.add_argument("--brightness", "-b", type=int, metavar="N")
    sp.add_argument("--kelvin", "-k", type=positive_int, metavar="K")
    sp.add_argument("--on", action="store_true", help="also turn the light on")
    fade(sp)
    sp = sub.add_parser("preset", help="apply a named preset from config (turns the light on); no name lists them")
    sp.add_argument("name", nargs="?")
    fade(sp)
    sp = sub.add_parser("info", help="show accessory info (model, firmware, serial)")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("auto", help="take one auto-brightness measurement and show what it would do (sends nothing)")
    sp.add_argument("--save-frame", metavar="DIR", help="also write an annotated frame (face, Key Side, ROI, 10%% grid)")
    sp = sub.add_parser("discover", help="look for lights via mDNS (_elg._tcp)")
    sp.add_argument("--timeout", type=float, default=3.0)
    sp.add_argument("--json", action="store_true")
    return p


def run(args: argparse.Namespace, cfg: Config, out=print, err=None) -> int:
    err = err or (lambda m: print(f"keylight: {m}", file=sys.stderr))
    say = (lambda *_: None) if args.quiet else out

    if args.cmd == "discover":
        services = mdns.discover(args.timeout)
        if args.json:
            out(json.dumps([{"instance": s.instance, "address": s.address, "port": s.port, "txt": s.txt,
                             "supported": not s.uses_tls} for s in services], indent=2))
        elif not services:
            out("no _elg._tcp services found (is the light on another VLAN? mDNS doesn't cross subnets by default)")
        for s in services if not args.json else []:
            note = "  UNSUPPORTED: tls= (mTLS protocol)" if s.uses_tls else ""
            out(f"{s.instance}  {s.address}:{s.port}  model={s.txt.get('md', '?')}{note}")
        return 0

    if args.cmd == "auto":
        return run_auto(args, cfg, out, err)

    if args.cmd == "preset" and args.name is None:
        for name, pr in cfg.presets.items():
            out(f"{name}: brightness={pr.brightness} kelvin={pr.kelvin}")
        return 0

    def op(light: KeyLight) -> int:
        cmd = args.cmd
        if cmd == "status":
            st = light.get_state()
            out(json.dumps(st.to_dict()) if args.json else describe(st))
            return 0
        if cmd == "info":
            info = light.info()
            if args.json:
                out(json.dumps(info, indent=2))
            else:
                for key in ("productName", "hardwareBoardType", "firmwareVersion", "firmwareBuildNumber",
                            "serialNumber", "displayName"):
                    if key in info:
                        out(f"{key}: {info[key]}")
                wifi = info.get("wifi-info") or {}
                if wifi:
                    out(f"wifi: {wifi.get('ssid', '?')} {wifi.get('frequencyMHz', '?')} MHz rssi {wifi.get('rssi', '?')}")
            return 0
        if cmd == "on":
            st = light.set_state(on=True)
        elif cmd == "off":
            st = light.set_state(on=False)
        elif cmd == "toggle":
            st = light.set_state(on=not light.get_state().on)
        elif cmd == "brightness":
            kind, value = args.value
            if kind == "abs" and value == 0:
                st = light.set_state(on=False)
            else:
                target = light.get_state().brightness + value if kind == "rel" else value
                st = light.fade_to(brightness=target, duration_ms=args.fade_ms)
        elif cmd == "temp":
            st = light.fade_to(mired=kelvin_to_mired(args.kelvin), duration_ms=args.fade_ms)
        elif cmd == "set":
            if args.brightness is None and args.kelvin is None and not args.on:
                err("set: give --brightness and/or --kelvin")
                return 2
            st = light.fade_to(brightness=args.brightness,
                               mired=kelvin_to_mired(args.kelvin) if args.kelvin else None,
                               duration_ms=args.fade_ms, on=True if args.on else None)
        elif cmd == "preset":
            preset = cfg.presets.get(args.name)
            if preset is None:
                err(f"unknown preset {args.name!r}; defined: {', '.join(cfg.presets) or 'none'}")
                return 2
            st = light.apply_preset(preset, duration_ms=args.fade_ms)
        else:  # pragma: no cover - argparse guarantees a known command
            raise AssertionError(cmd)
        say(describe(st))
        return 0

    return run_with_fallback(cfg, op, host=args.host, warn=err)


def run_auto(args: argparse.Namespace, cfg: Config, out, err) -> int:
    from datetime import datetime
    from pathlib import Path

    from .auto import closed_loop, open_loop
    from .sun import schedule_brightness, solar_elevation

    now = datetime.now().astimezone()
    loc, a = cfg.location, cfg.auto
    scheduled = schedule_brightness(now, loc.latitude, loc.longitude, a.day_brightness, a.night_brightness)
    if loc.latitude is not None:
        out(f"sun: elevation {solar_elevation(now, loc.latitude, loc.longitude):.1f} deg, schedule {scheduled}%")
    else:
        out(f"sun: no [location] set, schedule = day_brightness {scheduled}%")
    state = run_with_fallback(cfg, lambda light: light.get_state(), host=args.host, warn=err)
    out(f"light: {describe(state)}")
    try:
        from .auto import ObsFrameSource
        source = ObsFrameSource(cfg)
    except (ImportError, OSError, KeylightError, ValueError) as e:
        out(f"frame source: none ({e}) -> open-loop")
        d = open_loop(state.brightness, scheduled, a)
        out(f"would set: {d.current}% -> {d.target}%")
        return 0
    try:
        img = source.frame()
        m = source.vision.measure(img)
    finally:
        source.close()
    out(f"frame source: OBS ({source.source!r}, {img.shape[1]}x{img.shape[0]})")
    if m.face_lin is None:
        out("face: none found -> would hold")
    else:
        d = closed_loop(state.brightness, m, a)
        bg = f"{m.bg_lin:.3f}" if m.bg_lin is not None else "-"
        ratio = f" ({__import__('math').log2(m.face_lin / m.bg_lin):+.2f} stops vs background)" if m.bg_lin else ""
        out(f"face (Key Side, linear): {m.face_lin:.3f}  background: {bg}{ratio}  score {m.score:.2f}")
        out(f"would set: {d.current}% -> {d.target}% (error {d.error_stops:+.2f} stops{', ' + d.note if d.note else ''})")
    if args.save_frame:
        path = Path(args.save_frame).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        path = path / f"keylight-{now:%Y%m%d-%H%M%S}.jpg"
        source.vision.annotate(img, m, path)
        out(f"saved {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        return run(args, cfg)
    except KeylightError as e:
        print(f"keylight: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
