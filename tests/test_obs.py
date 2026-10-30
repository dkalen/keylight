"""ObsClient against a fake obs-websocket v5 server (real sockets, real RFC 6455 framing)."""

import base64
import hashlib
import json
import socket
import struct
import threading
import unittest
from unittest import mock

from keylight import obs
from keylight.core import ObsConfig
from keylight.obs import ObsClient, ObsError, ObsUnavailable, auth_response

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
JPEG = b"\xff\xd8fake-jpeg\xff\xd9" * 5000     # > 64 KiB once base64'd: exercises 64-bit lengths


class FakeObs:
    def __init__(self, password=None):
        self.password = password
        self.recording = False
        self.streaming = False
        self.inputs = [{"inputName": "Audio", "inputKind": "pulse_input_capture"},
                       {"inputName": "Video Capture Device (V4L2)", "inputKind": "v4l2_input"}]
        self.requests = []
        self.active: set[str] = set()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.sock.close()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def _read_frame(self, conn):
        b0, b1 = self._recv_exact(conn, 2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._recv_exact(conn, 2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._recv_exact(conn, 8))[0]
        assert b1 & 0x80, "client frames must be masked"
        mask = self._recv_exact(conn, 4)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._recv_exact(conn, n)))
        return b0 & 0x0F, data

    @staticmethod
    def _frame(opcode, payload, fragment_at=None):
        def one(op, data, fin):
            n = len(data)
            head = bytes([(0x80 if fin else 0) | op])
            if n < 126:
                head += bytes([n])
            elif n < 1 << 16:
                head += bytes([126]) + struct.pack("!H", n)
            else:
                head += bytes([127]) + struct.pack("!Q", n)
            return head + data
        if fragment_at:
            return one(opcode, payload[:fragment_at], False) + one(0, payload[fragment_at:], True)
        return one(opcode, payload, True)

    def _send(self, conn, msg, **kw):
        conn.sendall(self._frame(0x1, json.dumps(msg).encode(), **kw))

    def _serve(self, conn):
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                req += conn.recv(4096)
            headers = {k.strip().lower(): v.strip() for k, _, v in
                       (l.partition(":") for l in req.decode().split("\r\n")[1:] if l)}
            accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: obswebsocket.json\r\n\r\n").encode())
            hello = {"obsWebSocketVersion": "5.6.0", "rpcVersion": 1}
            if self.password:
                hello["authentication"] = {"challenge": "chal", "salt": "salt"}
            self._send(conn, {"op": 0, "d": hello})
            conn.sendall(self._frame(0x9, b"ping"))           # client must answer pings
            op, data = self._read_frame(conn)
            ident = json.loads(data)["d"]
            if self.password and ident.get("authentication") != auth_response(self.password, "salt", "chal"):
                conn.sendall(self._frame(0x8, struct.pack("!H", 4009) + b"Authentication failed."))
                return
            self._send(conn, {"op": 2, "d": {"negotiatedRpcVersion": 1}})
            while True:
                op, data = self._read_frame(conn)
                if op == 0xA:
                    continue
                if op == 0x8:
                    return
                d = json.loads(data)["d"]
                self.requests.append(d)
                self._respond(conn, d)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            conn.close()

    def _respond(self, conn, d):
        rt, rid = d["requestType"], d["requestId"]
        ok, data, kw = True, {}, {}
        if rt == "GetRecordStatus":
            data = {"outputActive": self.recording}
        elif rt == "GetStreamStatus":
            data = {"outputActive": self.streaming}
        elif rt == "GetSourceActive":
            name = d["requestData"]["sourceName"]
            ok = name in [i["inputName"] for i in self.inputs]
            data = {"videoActive": name in self.active, "videoShowing": name in self.active}
        elif rt == "GetInputList":
            data = {"inputs": self.inputs}
        elif rt == "GetSourceScreenshot":
            if d["requestData"]["sourceName"] not in [i["inputName"] for i in self.inputs]:
                ok = False
            data = {"imageData": "data:image/jpg;base64," + base64.b64encode(JPEG).decode()}
            kw = {"fragment_at": 1000}
        else:
            ok = False
        status = {"result": ok, "code": 100 if ok else 600, **({} if ok else {"comment": "nope"})}
        self._send(conn, {"op": 7, "d": {"requestType": rt, "requestId": rid, "requestStatus": status,
                                         "responseData": data}}, **kw)


class ObsClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeObs()

    def tearDown(self):
        self.server.close()

    def cfg(self, **kw):
        return ObsConfig(host="127.0.0.1", port=self.server.port, timeout=2.0, **kw)

    def test_status(self):
        c = ObsClient(self.cfg(password=""))
        self.assertFalse(c.recording_or_streaming())
        self.server.streaming = True
        self.assertTrue(c.recording_or_streaming())
        c.close()

    def test_capture_source_autodetect_and_screenshot(self):
        c = ObsClient(self.cfg(password=""))
        source = c.capture_source()
        self.assertEqual(source, "Video Capture Device (V4L2)")
        self.assertEqual(c.screenshot_jpeg(source, 480), JPEG)
        self.assertEqual(self.server.requests[-1]["requestData"]["imageWidth"], 480)
        c.close()

    def test_source_active(self):
        c = ObsClient(self.cfg(password=""))
        src = c.capture_source()
        self.assertFalse(c.source_active(src))
        self.server.active.add(src)
        self.assertTrue(c.source_active(src))
        with self.assertRaises(ObsError):
            c.source_active("missing")
        c.close()

    def test_camera_kinds(self):
        from keylight.obs import is_camera_kind
        for kind in ("v4l2_input", "av_capture_input", "av_capture_input_v2", "macos-avcapture",
                     "macos-avcapture-fast", "dshow_input"):
            self.assertTrue(is_camera_kind(kind), kind)
        for kind in ("screen_capture", "display_capture", "window_capture", "pulse_input_capture",
                     "coreaudio_input_capture", "sck_audio_capture", "image_source"):
            self.assertFalse(is_camera_kind(kind), kind)

    def test_request_failure(self):
        c = ObsClient(self.cfg(password=""))
        with self.assertRaises(ObsError):
            c.screenshot_jpeg("missing", 480)
        c.close()

    def test_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(ObsUnavailable):
            ObsClient(ObsConfig(host="127.0.0.1", port=port, timeout=1.0, password=""))


class ObsAuthTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeObs(password="s3cret")

    def tearDown(self):
        self.server.close()

    def cfg(self, **kw):
        return ObsConfig(host="127.0.0.1", port=self.server.port, timeout=2.0, **kw)

    def test_auth_ok(self):
        ObsClient(self.cfg(password="s3cret")).close()

    def test_auth_wrong(self):
        with self.assertRaises(ObsError) as cm:
            ObsClient(self.cfg(password="wrong"))
        self.assertIn("password", str(cm.exception))

    def test_password_from_obs_config(self):
        with mock.patch.object(obs, "obs_websocket_config_path") as path:
            path.return_value.read_text.return_value = json.dumps({"auth_required": True, "server_password": "s3cret"})
            ObsClient(self.cfg()).close()

    def test_password_missing(self):
        with mock.patch.object(obs, "obs_websocket_config_path") as path:
            path.return_value.read_text.side_effect = OSError
            with self.assertRaises(ObsError):
                ObsClient(self.cfg())


if __name__ == "__main__":
    unittest.main()
