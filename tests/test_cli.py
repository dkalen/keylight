import contextlib
import io
import json
import os
import socket
import tempfile
import unittest

from keylight import cli

from .mock_light import MockLight


class CliTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockLight().__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.tmp.name, "config.toml")
        self.write_config(self.mock.port)

    def tearDown(self):
        self.mock.__exit__()
        self.tmp.cleanup()

    def write_config(self, port):
        with open(self.config, "w") as f:
            f.write(f"""
[light]
host = "127.0.0.1"
port = {port}
timeout = 0.5
mdns_fallback = false

[presets.call]
brightness = 50
kelvin = 5000
""")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", self.config, *args])
        return code, out.getvalue(), err.getvalue()

    def test_status_text_and_json(self):
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("off", out)
        self.assertIn("40%", out)
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(json.loads(out), {"on": False, "brightness": 40, "kelvin": 4695, "mired": 213})

    def test_on_off_toggle(self):
        self.assertEqual(self.run_cli("on")[0], 0)
        self.assertEqual(self.mock.state["on"], 1)
        self.run_cli("toggle")
        self.assertEqual(self.mock.state["on"], 0)
        self.run_cli("toggle")
        self.assertEqual(self.mock.state["on"], 1)
        self.run_cli("off")
        self.assertEqual(self.mock.state["on"], 0)

    def test_brightness_absolute_relative(self):
        self.run_cli("brightness", "70")
        self.assertEqual(self.mock.state["brightness"], 70)
        self.run_cli("brightness", "+10")
        self.assertEqual(self.mock.state["brightness"], 80)
        self.run_cli("brightness", "-50")
        self.assertEqual(self.mock.state["brightness"], 30)
        self.run_cli("brightness", "-90")
        self.assertEqual(self.mock.state["brightness"], 1)     # clamped at minimum, not off

    def test_brightness_zero_turns_off(self):
        self.mock.state["on"] = 1
        self.run_cli("brightness", "0")
        self.assertEqual(self.mock.state["on"], 0)
        self.assertEqual(self.mock.state["brightness"], 40)

    def test_brightness_out_of_range(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--config", self.config, "brightness", "101"])
        self.assertEqual(cm.exception.code, 2)

    def test_temp_and_set(self):
        self.run_cli("temp", "5000")
        self.assertEqual(self.mock.state["temperature"], 200)
        self.run_cli("set", "--brightness", "25", "--kelvin", "4000")
        self.assertEqual((self.mock.state["brightness"], self.mock.state["temperature"]), (25, 250))
        self.assertEqual(self.mock.state["on"], 0)

    def test_preset(self):
        code, out, _ = self.run_cli("preset", "call")
        self.assertEqual(code, 0)
        self.assertEqual(self.mock.state, {"on": 1, "brightness": 50, "temperature": 200})
        code, _, err = self.run_cli("preset", "nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown preset", err)
        code, out, _ = self.run_cli("preset")
        self.assertIn("call: brightness=50", out)

    def test_quiet(self):
        code, out, _ = self.run_cli("-q", "on")
        self.assertEqual((code, out), (0, ""))

    def test_unreachable_exit_code(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.write_config(port)
        code, _, err = self.run_cli("on")
        self.assertEqual(code, 3)
        self.assertIn("unreachable", err)

    def test_info(self):
        code, out, _ = self.run_cli("info")
        self.assertEqual(code, 0)
        self.assertIn("Key Light MK.2", out)

    def test_bad_config_exit_code(self):
        with open(self.config, "w") as f:
            f.write("[light\n")
        self.assertEqual(self.run_cli("status")[0], 2)


if __name__ == "__main__":
    unittest.main()
