import socket
import struct
import unittest
from unittest import mock

from keylight import mdns
from keylight.core import UnsupportedDeviceError

INSTANCE = "Elgato Key Light MK.2 ABCD._elg._tcp.local"
TARGET = "elgato-key-light-mk2-abcd.local"


def rr(name: bytes, rtype: int, rdata: bytes) -> bytes:
    return name + struct.pack("!HHIH", rtype, 0x8001, 120, len(rdata)) + rdata


def txt(*entries: str) -> bytes:
    return b"".join(bytes([len(e)]) + e.encode() for e in entries)


def response(txt_entries=("mf=Elgato", "dt=205", "md=Elgato Key Light MK.2 20GAK9902", "id=3C:6A:9D:00:00:00")):
    # Use a compression pointer for the instance name in SRV/TXT, like real responders.
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 3)
    service = mdns.encode_name(mdns.SERVICE)
    ptr_off = 12 + len(service) + 10
    ptr = rr(service, mdns.T_PTR, mdns.encode_name(INSTANCE))
    ref = struct.pack("!H", 0xC000 | ptr_off)
    srv = rr(ref, mdns.T_SRV, struct.pack("!HHH", 0, 0, 9123) + mdns.encode_name(TARGET))
    tx = rr(ref, mdns.T_TXT, txt(*txt_entries))
    a = rr(mdns.encode_name(TARGET), mdns.T_A, socket.inet_aton("192.168.1.50"))
    return header + ptr + srv + tx + a


class MdnsTests(unittest.TestCase):
    def test_parse_and_assemble(self):
        services = list(mdns.assemble(mdns.parse_records(response())).values())
        self.assertEqual(len(services), 1)
        s = services[0]
        self.assertEqual(s.instance, INSTANCE)
        self.assertEqual((s.address, s.port), ("192.168.1.50", 9123))
        self.assertEqual(s.txt["dt"], "205")
        self.assertFalse(s.uses_tls)

    def test_tls_detected(self):
        s = list(mdns.assemble(mdns.parse_records(response(("mf=Elgato", "tls=1")))).values())[0]
        self.assertTrue(s.uses_tls)

    def test_find_light_rejects_tls(self):
        s = list(mdns.assemble(mdns.parse_records(response(("tls=1",)))).values())[0]
        with mock.patch.object(mdns, "discover", return_value=[s]):
            with self.assertRaises(UnsupportedDeviceError) as cm:
                mdns.find_light()
        self.assertIn("tls=", str(cm.exception))

    def test_find_light_none(self):
        with mock.patch.object(mdns, "discover", return_value=[]):
            self.assertIsNone(mdns.find_light())

    def test_query_roundtrip(self):
        q = mdns.build_query((mdns.SERVICE, mdns.T_PTR))
        name, off = mdns._read_name(q, 12)
        self.assertEqual(name, mdns.SERVICE)
        self.assertEqual(struct.unpack_from("!HH", q, off), (mdns.T_PTR, 1))

    def test_compression_loop_rejected(self):
        buf = b"\x00" * 12 + b"\xc0\x0c"
        with self.assertRaises(ValueError):
            mdns._read_name(buf, 12)


if __name__ == "__main__":
    unittest.main()
