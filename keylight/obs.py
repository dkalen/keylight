"""Minimal obs-websocket v5 client (standard library only).

Just enough of RFC 6455 for one client talking JSON to OBS on localhost:
the upgrade handshake, masked text frames out, text/continuation/ping/close in.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
from pathlib import Path

from .core import KeylightError, ObsConfig

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED, OP_REQUEST, OP_RESPONSE = 0, 1, 2, 6, 7


class ObsUnavailable(KeylightError):
    """OBS isn't running, its websocket server is off, or the connection dropped."""
    exit_code = 3


class ObsError(KeylightError):
    """OBS answered, but refused: bad password, failed request."""
    exit_code = 4


def obs_websocket_config_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "obs-studio"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "obs-studio"
    return base / "plugin_config" / "obs-websocket" / "config.json"


def resolve_password(cfg: ObsConfig) -> str | None:
    """Explicit password, then password_file, then OBS's own config on this machine."""
    if cfg.password is not None:
        return cfg.password
    if cfg.password_file:
        return Path(cfg.password_file).expanduser().read_text().strip()
    try:
        data = json.loads(obs_websocket_config_path().read_text())
    except (OSError, ValueError):
        return None
    return data.get("server_password") if data.get("auth_required", True) else None


def auth_response(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class WebSocket:
    def __init__(self, host: str, port: int, timeout: float, subprotocol: str):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise ObsUnavailable(f"OBS websocket at {host}:{port} is unreachable: {e}") from e
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Protocol: {subprotocol}\r\n\r\n")
        self._send_raw(request.encode())
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode(errors="replace").split("\r\n")
        if " 101 " not in lines[0] + " ":
            self.close()
            raise ObsUnavailable(f"websocket upgrade refused: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}
        expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            self.close()
            raise ObsUnavailable("websocket handshake failed (bad accept key)")

    def _send_raw(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise ObsUnavailable(f"OBS connection lost: {e}") from e

    def _fill(self) -> None:
        try:
            chunk = self.sock.recv(65536)
        except OSError as e:
            raise ObsUnavailable(f"OBS connection lost: {e}") from e
        if not chunk:
            raise ObsUnavailable("OBS closed the connection")
        self.buf += chunk

    def _take(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 1 << 16:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._send_raw(header + mask + masked)

    def send_text(self, text: str) -> None:
        self.send_frame(0x1, text.encode())

    def recv_text(self) -> str:
        parts: list[bytes] = []
        while True:
            b0, b1 = self._take(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._take(8))[0]
            mask = self._take(4) if b1 & 0x80 else None
            payload = self._take(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                self.close()
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                raise ObsUnavailable(f"OBS closed the connection ({code} {payload[2:].decode(errors='replace')})")
            if opcode == 0x9:
                self.send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x0):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class ObsClient:
    def __init__(self, cfg: ObsConfig):
        self.cfg = cfg
        self.ws = WebSocket(cfg.host, cfg.port, cfg.timeout, "obswebsocket.json")
        self._next_id = 0
        try:
            hello = self._recv_op(OP_HELLO)
            identify: dict = {"rpcVersion": 1, "eventSubscriptions": 0}
            auth = hello.get("authentication")
            if auth:
                password = resolve_password(cfg)
                if password is None:
                    raise ObsError("OBS websocket needs a password: set [obs] password/password_file, "
                                   "or run on the machine where OBS's own config holds it")
                identify["authentication"] = auth_response(password, auth["salt"], auth["challenge"])
            self.ws.send_text(json.dumps({"op": OP_IDENTIFY, "d": identify}))
            try:
                self._recv_op(OP_IDENTIFIED)
            except ObsUnavailable as e:
                if "4009" in str(e):
                    raise ObsError("OBS rejected the websocket password") from e
                raise
        except BaseException:
            self.ws.close()
            raise

    def _recv_op(self, op: int) -> dict:
        while True:
            msg = json.loads(self.ws.recv_text())
            if msg.get("op") == op:
                return msg.get("d", {})

    def request(self, request_type: str, data: dict | None = None) -> dict:
        self._next_id += 1
        rid = str(self._next_id)
        body: dict = {"requestType": request_type, "requestId": rid}
        if data:
            body["requestData"] = data
        self.ws.send_text(json.dumps({"op": OP_REQUEST, "d": body}))
        while True:
            d = self._recv_op(OP_RESPONSE)
            if d.get("requestId") == rid:
                break
        status = d.get("requestStatus", {})
        if not status.get("result"):
            raise ObsError(f"{request_type} failed: {status.get('code')} {status.get('comment', '')}".strip())
        return d.get("responseData") or {}

    def output_active(self, request_type: str) -> bool:
        return bool(self.request(request_type).get("outputActive"))

    def recording_or_streaming(self) -> bool:
        return self.output_active("GetRecordStatus") or self.output_active("GetStreamStatus")

    def capture_source(self) -> str:
        """Configured source, else the first video capture input."""
        if self.cfg.source:
            return self.cfg.source
        inputs = self.request("GetInputList").get("inputs", [])
        for item in inputs:
            kind = item.get("inputKind", "")
            if kind in ("v4l2_input", "av_capture_input", "av_capture_input_v2", "macos-avcapture", "dshow_input"):
                return item["inputName"]
        raise ObsError("no video capture input found in OBS; set [obs] source")

    def screenshot_jpeg(self, source: str, width: int) -> bytes:
        data = self.request("GetSourceScreenshot", {"sourceName": source, "imageFormat": "jpg",
                                                    "imageWidth": width, "imageCompressionQuality": 85})
        uri = data.get("imageData", "")
        _, _, b64 = uri.partition(",")
        try:
            return base64.b64decode(b64)
        except ValueError as e:
            raise ObsError("GetSourceScreenshot returned undecodable data") from e

    def close(self) -> None:
        self.ws.close()
