import logging
import math
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from keylight.auto import AutoBrightness, Measurement, closed_loop, open_loop, srgb_to_linear, stops_step
from keylight.core import AutoConfig, KeyLight, KeylightError, parse_config

from .mock_light import MockLight


class MathTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AutoConfig()

    def test_srgb(self):
        self.assertAlmostEqual(srgb_to_linear(0), 0)
        self.assertAlmostEqual(srgb_to_linear(1), 1)
        self.assertAlmostEqual(srgb_to_linear(0.5), 0.214, places=3)

    def test_deadband(self):
        self.assertEqual(stops_step(40, 0.1, self.cfg), 40)
        self.assertEqual(stops_step(40, -0.14, self.cfg), 40)

    def test_step_limited(self):
        # +3 stops of error, but at most 1/3 stop per update
        self.assertEqual(stops_step(40, 3.0, self.cfg), round(40 * 2 ** 0.33))
        self.assertEqual(stops_step(40, -3.0, self.cfg), round(40 * 2 ** -0.33))

    def test_gain(self):
        self.assertEqual(stops_step(40, 0.4, self.cfg), round(40 * 2 ** 0.2))

    def test_low_levels_still_move(self):
        self.assertEqual(stops_step(5, 0.3, AutoConfig(min_brightness=1)), 6)

    def test_clamped(self):
        self.assertEqual(stops_step(79, 3.0, self.cfg), 80)
        self.assertEqual(stops_step(5, -3.0, self.cfg), 5)

    def test_closed_loop_toward_target(self):
        target = srgb_to_linear(self.cfg.target_luma)
        d = closed_loop(40, Measurement(face_lin=target / 2), self.cfg)      # 1 stop under
        self.assertAlmostEqual(d.error_stops, 1.0)
        self.assertGreater(d.target, 40)
        d = closed_loop(40, Measurement(face_lin=target), self.cfg)
        self.assertEqual(d.target, 40)

    def test_ratio_raises_target_for_bright_background(self):
        target = srgb_to_linear(self.cfg.target_luma)
        bright_bg = target * 8                       # face at target is 3 stops under
        d = closed_loop(40, Measurement(face_lin=target, bg_lin=bright_bg), self.cfg)
        self.assertAlmostEqual(d.error_stops, 2.0)   # allowed 1 stop under
        self.assertEqual(d.note, "ratio")
        no_ratio = closed_loop(40, Measurement(face_lin=target, bg_lin=bright_bg), AutoConfig(ratio_mode=False))
        self.assertEqual(no_ratio.target, 40)

    def test_open_loop_never_overshoots(self):
        self.assertEqual(open_loop(40, 42, self.cfg).target, 40)     # inside the deadband
        self.assertEqual(open_loop(40, 46, self.cfg).target, 43)
        self.assertEqual(open_loop(5, 6, self.cfg).target, 6)       # +1 nudge, capped at schedule
        d = open_loop(40, 20, self.cfg)
        self.assertEqual(d.target, round(40 * 2 ** -0.33))


class FakeSource:
    name = "fake"

    def __init__(self, measurements):
        self.measurements = list(measurements)
        self.closed = False

    def measure(self):
        m = self.measurements.pop(0)
        if isinstance(m, Exception):
            raise m
        return m

    def close(self):
        self.closed = True


class ControllerTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.mock = MockLight().__enter__()
        self.mock.state.update(on=1, brightness=40)
        self.light = KeyLight("127.0.0.1", self.mock.port, timeout=0.5)
        self.t = 0.0
        self.cfg = parse_config({"location": {"latitude": 40.71, "longitude": -74.01},
                                 "auto": {"fade_ms": 0, "day_brightness": 45, "night_brightness": 20}})
        self.target = srgb_to_linear(self.cfg.auto.target_luma)

    def tearDown(self):
        self.mock.__exit__()
        logging.disable(logging.NOTSET)

    def make(self, source=None, factory=None, hour=13):
        when = datetime(2026, 6, 21, hour, 0, tzinfo=ZoneInfo("America/New_York"))
        self.auto = AutoBrightness(self.cfg, runner=lambda fn: fn(self.light),
                                   source_factory=factory or (lambda cfg: source),
                                   clock=lambda: self.t, now=lambda: when)
        return self.auto

    def test_closed_loop_brightens_dim_face(self):
        a = self.make(FakeSource([Measurement(face_lin=self.target / 2)] * 3))
        d = a.cycle()
        self.assertEqual(d.mode, "closed-loop")
        self.assertGreater(self.mock.state["brightness"], 40)

    def test_open_loop_without_source(self):
        def factory(cfg):
            raise ImportError("No module named 'cv2'")
        a = self.make(factory=factory, hour=1)          # night: schedule says 20
        d = a.cycle()
        self.assertEqual(d.mode, "open-loop")
        self.assertLess(self.mock.state["brightness"], 40)

    def test_no_face_holds_then_falls_back(self):
        a = self.make(FakeSource([Measurement(face_lin=None)] * 5), hour=1)
        self.assertEqual(a.cycle().mode, "hold")
        self.assertEqual(a.cycle().mode, "hold")
        self.assertEqual(a.cycle().mode, "open-loop")

    def test_lost_source_falls_back_and_closes(self):
        src = FakeSource([KeylightError("gone")])
        a = self.make(src, hour=1)
        self.assertEqual(a.cycle().mode, "open-loop")
        self.assertTrue(src.closed)

    def test_manual_change_pauses(self):
        a = self.make(FakeSource([Measurement(face_lin=self.target / 2)] * 5))
        a.cycle()
        sent = self.mock.state["brightness"]
        self.mock.state["brightness"] = 70              # hotkey
        self.assertIsNone(a.cycle())
        self.t += 60
        self.assertIsNone(a.cycle())                    # still paused
        self.t += self.cfg.auto.manual_pause_min * 60
        self.assertIsNotNone(a.cycle())
        self.assertNotEqual(sent, 70)

    def test_light_turned_off_by_user_is_left_off(self):
        a = self.make(FakeSource([Measurement(face_lin=self.target / 2)] * 5))
        a.cycle()
        self.mock.state["on"] = 0
        self.assertIsNone(a.cycle())
        self.assertEqual(self.mock.state["on"], 0)

    def test_dry_run_sends_nothing(self):
        a = self.make(FakeSource([Measurement(face_lin=self.target / 4)] * 3))
        a.dry_run = True
        d = a.cycle()
        self.assertGreater(d.target, 40)
        self.assertEqual(self.mock.puts(), [])

    def test_median_smoothing(self):
        a = self.make(FakeSource([Measurement(face_lin=self.target)] * 2 + [Measurement(face_lin=self.target / 16)]))
        a.cycle()
        a.cycle()
        d = a.cycle()                                   # one outlier frame doesn't move the light
        self.assertEqual(d.target, d.current)


@unittest.skipUnless(__import__("importlib").util.find_spec("cv2"), "opencv not installed")
class VisionTests(unittest.TestCase):
    def test_key_side_box(self):
        from keylight.vision import Vision
        v = Vision.__new__(Vision)
        v.cfg = AutoConfig(key_side="left", mirrored=False)
        # subject's left = image right half of the inner box
        self.assertEqual(v.key_side_box((100, 100, 100, 100), 640), (150, 120, 30, 60))
        v.cfg = AutoConfig(key_side="left", mirrored=True)
        self.assertEqual(v.key_side_box((100, 100, 100, 100), 640), (120, 120, 30, 60))

    def test_linear_luma(self):
        import numpy as np
        from keylight.vision import Vision
        v = Vision.__new__(Vision)
        v.lut = np.array([srgb_to_linear(i / 255) for i in range(256)], dtype=np.float32)
        img = np.full((4, 4, 3), 255, dtype=np.uint8)
        self.assertTrue(math.isclose(float(v.linear_luma(img).mean()), 1.0, rel_tol=1e-5))


if __name__ == "__main__":
    unittest.main()
