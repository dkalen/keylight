import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from keylight.sun import daylight_fraction, schedule_brightness, solar_elevation

NY = ZoneInfo("America/New_York")
NYC = (40.71, -74.01)


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=NY)


class SunTests(unittest.TestCase):
    def test_noon_elevations(self):
        # 90 - latitude + declination, at local solar noon
        self.assertAlmostEqual(solar_elevation(at("2026-06-21 12:58"), *NYC), 72.7, delta=0.3)
        self.assertAlmostEqual(solar_elevation(at("2026-03-20 13:04"), *NYC), 49.3, delta=0.5)
        self.assertAlmostEqual(solar_elevation(at("2026-12-21 11:54"), *NYC), 25.85, delta=0.3)

    def test_night_is_negative(self):
        self.assertLess(solar_elevation(at("2026-10-29 00:00"), *NYC), -50)

    def test_sunset_near_horizon(self):
        # New York sunset on 2026-10-29 is about 17:52 EDT
        self.assertAlmostEqual(solar_elevation(at("2026-10-29 17:52"), *NYC), 0, delta=1.0)

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            solar_elevation(datetime(2026, 1, 1), *NYC)

    def test_daylight_fraction_ramp(self):
        self.assertEqual(daylight_fraction(-10), 0)
        self.assertEqual(daylight_fraction(-6), 0)
        self.assertEqual(daylight_fraction(20), 1)
        self.assertAlmostEqual(daylight_fraction(7), 0.5)

    def test_schedule(self):
        self.assertEqual(schedule_brightness(at("2026-06-21 13:00"), *NYC, 45, 20), 45)
        self.assertEqual(schedule_brightness(at("2026-06-21 01:00"), *NYC, 45, 20), 20)
        dusk = schedule_brightness(at("2026-10-29 17:52"), *NYC, 45, 20)
        self.assertTrue(20 < dusk < 45)

    def test_schedule_without_location(self):
        self.assertEqual(schedule_brightness(at("2026-06-21 01:00"), None, None, 45, 20), 45)


if __name__ == "__main__":
    unittest.main()
