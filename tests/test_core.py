import os
import socket
import unittest
from pathlib import Path
from unittest import mock

from keylight.core import (ConfigError, KeyLight, Preset, UnreachableError, UnsupportedDeviceError,
                           kelvin_to_mired, load_config, mired_to_kelvin, parse_config)

from .mock_light import MockLight


class ConversionTests(unittest.TestCase):
    def test_kelvin_to_mired(self):
        self.assertEqual(kelvin_to_mired(5000), 200)
        self.assertEqual(kelvin_to_mired(7000), 143)
        self.assertEqual(kelvin_to_mired(2900), 344)   # 344.8 rounds to 345, clamped

    def test_kelvin_clamped(self):
        self.assertEqual(kelvin_to_mired(10000), 143)
        self.assertEqual(kelvin_to_mired(1000), 344)

    def test_mired_to_kelvin(self):
        self.assertEqual(mired_to_kelvin(200), 5000)
        self.assertEqual(mired_to_kelvin(0), 0)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = parse_config({})
        self.assertIsNone(cfg.host)
        self.assertEqual(cfg.port, 9123)
        self.assertEqual(cfg.presets, {})
        self.assertEqual(cfg.watch.on_action, "auto")
        self.assertIsNone(cfg.kelvin)

    def test_phase2_sections(self):
        cfg = parse_config({
            "light": {"kelvin": 4700},
            "location": {"latitude": 40.71, "longitude": -74.01},
            "obs": {"port": 4456, "source": "cam"},
            "auto": {"interval": 5, "background_roi": [0, 0, 0.3, 1], "ratio_mode": False},
            "watch": {"linux_virtual_devices": ["name:OBS Virtual Camera"], "obs_sessions": False},
        })
        self.assertEqual(cfg.kelvin, 4700)
        self.assertEqual((cfg.location.latitude, cfg.location.longitude), (40.71, -74.01))
        self.assertEqual((cfg.obs.port, cfg.obs.source), (4456, "cam"))
        self.assertEqual(cfg.auto.interval, 5.0)
        self.assertEqual(cfg.auto.background_roi, [0.0, 0.0, 0.3, 1.0])
        self.assertFalse(cfg.auto.ratio_mode)
        self.assertEqual(cfg.watch.linux_virtual_devices, ["name:OBS Virtual Camera"])
        self.assertFalse(cfg.watch.obs_sessions)

    def test_phase2_validation(self):
        for bad in ({"auto": {"nope": 1}}, {"auto": {"background_roi": [0, 0, 2, 1]}},
                    {"auto": {"key_side": "up"}}, {"location": {"latitude": 40.7}},
                    {"auto": {"interval": "6"}}, {"watch": {"linux_virtual_devices": [1]}}):
            with self.assertRaises(ConfigError, msg=bad):
                parse_config(bad)

    def test_full(self):
        cfg = parse_config({
            "light": {"host": "10.0.0.5", "timeout": 2, "min_brightness": 5},
            "presets": {"call": {"brightness": 50, "kelvin": 4500}, "dim": {"brightness": 10}},
            "watch": {"linux_devices": ["usb-*"], "on_delay": 1, "ignore_processes": ["obs"]},
        })
        self.assertEqual(cfg.host, "10.0.0.5")
        self.assertEqual(cfg.timeout, 2.0)
        self.assertEqual(cfg.presets["dim"], Preset(brightness=10))
        self.assertEqual(cfg.watch.on_delay, 1.0)
        self.assertEqual(cfg.watch.ignore_processes, ["obs"])

    def test_bad_types(self):
        with self.assertRaises(ConfigError):
            parse_config({"light": {"port": "9123"}})
        with self.assertRaises(ConfigError):
            parse_config({"light": {"port": True}})
        with self.assertRaises(ConfigError):
            parse_config({"presets": {"x": {}}})

    def test_on_action_must_name_preset(self):
        parse_config({"watch": {"on_action": "restore"}})
        with self.assertRaises(ConfigError):
            parse_config({"watch": {"on_action": "preset:nope"}})
        with self.assertRaises(ConfigError):
            parse_config({"watch": {"on_action": "call"}})


class LayeredConfigTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "config.toml").write_text('[light]\nhost = "10.0.0.5"\nkelvin = 4700\n[auto]\ninterval = 5\n')
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("KEYLIGHT_LOCAL_CONFIG", "KEYLIGHT_LATITUDE", "KEYLIGHT_LONGITUDE"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_local_toml_merges_over_shared(self):
        (self.dir / "local.toml").write_text('[location]\nlatitude = 1.5\nlongitude = 2.5\n[auto]\nday_brightness = 60\n')
        cfg = load_config(self.dir / "config.toml")
        self.assertEqual((cfg.location.latitude, cfg.location.longitude), (1.5, 2.5))
        self.assertEqual((cfg.auto.interval, cfg.auto.day_brightness), (5.0, 60))   # both files' keys survive
        self.assertEqual(cfg.host, "10.0.0.5")

    def test_env_overrides_location(self):
        (self.dir / "local.toml").write_text('[location]\nlatitude = 1.5\nlongitude = 2.5\n')
        os.environ.update(KEYLIGHT_LATITUDE="40.7", KEYLIGHT_LONGITUDE="-74.0")
        cfg = load_config(self.dir / "config.toml")
        self.assertEqual((cfg.location.latitude, cfg.location.longitude), (40.7, -74.0))

    def test_local_config_env_path(self):
        other = self.dir / "elsewhere.toml"
        other.write_text('[location]\nlatitude = 3\nlongitude = 4\n')
        os.environ["KEYLIGHT_LOCAL_CONFIG"] = str(other)
        self.assertEqual(load_config(self.dir / "config.toml").location.latitude, 3.0)

    def test_bad_env(self):
        os.environ["KEYLIGHT_LATITUDE"] = "north"
        with self.assertRaises(ConfigError):
            load_config(self.dir / "config.toml")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockLight().__enter__()
        self.light = KeyLight("127.0.0.1", self.mock.port, timeout=1.0, retries=1)

    def tearDown(self):
        self.mock.__exit__()

    def test_get_state(self):
        st = self.light.get_state()
        self.assertFalse(st.on)
        self.assertEqual((st.brightness, st.mired, st.kelvin), (40, 213, 4695))

    def test_set_partial(self):
        st = self.light.set_state(on=True)
        self.assertTrue(st.on)
        self.assertEqual(self.mock.puts(), [{"on": 1}])
        self.assertEqual(self.mock.requests[-1][3]["numberOfLights"], 1)

    def test_never_sends_origin(self):
        self.light.get_state()
        self.light.set_state(brightness=50)
        self.light.info()
        for _, _, headers, _ in self.mock.requests:
            self.assertNotIn("Origin", headers)

    def test_clamps(self):
        self.light.set_state(brightness=0, mired=500)
        self.assertEqual(self.mock.puts(), [{"brightness": 1, "temperature": 344}])
        self.light.set_state(brightness=150, mired=10)
        self.assertEqual(self.mock.puts()[-1], {"brightness": 100, "temperature": 143})

    def test_retry_once(self):
        self.mock.fail_next = 1
        self.assertEqual(self.light.get_state().brightness, 40)
        self.assertEqual(len(self.mock.requests), 2)

    def test_gives_up_after_retry(self):
        self.mock.fail_next = 2
        with self.assertRaises(UnreachableError):
            self.light.get_state()

    def test_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(UnreachableError) as cm:
            KeyLight("127.0.0.1", port, timeout=0.5).get_state()
        self.assertEqual(cm.exception.exit_code, 3)

    def test_http_error_is_unsupported(self):
        with self.assertRaises(UnsupportedDeviceError) as cm:
            self.light._request("GET", "/elgato/nope")
        self.assertIn("404", str(cm.exception))

    def test_bad_payload(self):
        self.mock.state.pop("temperature")
        with self.assertRaises(UnsupportedDeviceError):
            self.light.get_state()

    def test_info(self):
        self.assertEqual(self.light.info()["productName"], "Elgato Key Light MK.2")

    def test_fade_steps_when_on(self):
        self.mock.state.update(on=1, brightness=20)
        sleeps = []
        st = self.light.fade_to(brightness=60, duration_ms=400, sleep=sleeps.append, step_ms=100)
        self.assertEqual(st.brightness, 60)
        self.assertEqual([p["brightness"] for p in self.mock.puts()], [30, 40, 50, 60])
        self.assertEqual(len(sleeps), 3)

    def test_fade_when_off_is_single_put(self):
        self.light.fade_to(brightness=60, duration_ms=400, sleep=lambda _: None)
        self.assertEqual(self.mock.puts(), [{"brightness": 60}])

    def test_preset_turns_on(self):
        st = self.light.apply_preset(Preset(brightness=35, kelvin=5000))
        self.assertTrue(st.on)
        self.assertEqual(self.mock.puts(), [{"on": 1, "brightness": 35, "temperature": 200}])


if __name__ == "__main__":
    unittest.main()
