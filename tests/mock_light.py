"""A fake Key Light: serves /elgato/lights and /elgato/accessory-info on localhost."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockLight:
    def __init__(self, reject_origin: bool = True):
        self.state = {"on": 0, "brightness": 40, "temperature": 213}
        self.info = {
            "productName": "Elgato Key Light MK.2",
            "hardwareBoardType": 205,
            "firmwareBuildNumber": 229,
            "firmwareVersion": "1.0.3",
            "serialNumber": "TEST000000",
            "displayName": "",
            "features": ["lights"],
            "wifi-info": {"ssid": "test", "frequencyMHz": 2400, "rssi": -50},
        }
        self.requests: list[tuple[str, str, dict, object]] = []   # (method, path, headers, body)
        self.reject_origin = reject_origin
        self.fail_next = 0          # drop this many connections before answering
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, code: int, payload: object) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self, body: object) -> bool:
                mock.requests.append((self.command, self.path, dict(self.headers), body))
                if mock.fail_next > 0:
                    mock.fail_next -= 1
                    self.close_connection = True
                    self.connection.close()
                    return False
                if mock.reject_origin and "Origin" in self.headers:
                    self._send(403, {"error": "origin not allowed"})
                    return False
                return True

            def do_GET(self):
                if not self._record(None):
                    return
                if self.path == "/elgato/lights":
                    self._send(200, {"numberOfLights": 1, "lights": [dict(mock.state)]})
                elif self.path == "/elgato/accessory-info":
                    self._send(200, mock.info)
                else:
                    self._send(404, {})

            def do_PUT(self):
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length))
                except ValueError:
                    body = None
                if not self._record(body):
                    return
                if self.path != "/elgato/lights":
                    self._send(404, {})
                    return
                try:
                    light = body["lights"][0]
                except (TypeError, KeyError, IndexError):
                    self._send(400, {})
                    return
                if "brightness" in light and not 0 <= light["brightness"] <= 100:
                    self._send(400, {})
                    return
                if "temperature" in light and not 143 <= light["temperature"] <= 344:
                    self._send(400, {})
                    return
                for key in ("on", "brightness", "temperature"):
                    if key in light:
                        mock.state[key] = int(light[key])
                self._send(200, {"numberOfLights": 1, "lights": [dict(mock.state)]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)

    def __enter__(self) -> "MockLight":
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()

    def puts(self) -> list[dict]:
        return [b["lights"][0] for m, p, _, b in self.requests if m == "PUT"]
