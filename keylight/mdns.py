"""Minimal mDNS browser for `_elg._tcp.local` (standard library only).

Sends one-shot "legacy unicast" queries from an ephemeral port (RFC 6762
§6.7), so it works alongside avahi/mDNSResponder already owning port 5353.
Only used as a fallback; a static IP in config is preferred.
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field

from .core import UnsupportedDeviceError

SERVICE = "_elg._tcp.local"
MDNS_ADDR = ("224.0.0.251", 5353)
T_A, T_PTR, T_TXT, T_SRV = 1, 12, 16, 33


@dataclass
class Service:
    instance: str
    target: str | None = None
    port: int | None = None
    addresses: list[str] = field(default_factory=list)
    txt: dict[str, str] = field(default_factory=dict)

    @property
    def address(self) -> str | None:
        return self.addresses[0] if self.addresses else None

    @property
    def uses_tls(self) -> bool:
        return "tls" in self.txt


# --------------------------------------------------------------------------- wire format

def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode()
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def build_query(*questions: tuple[str, int]) -> bytes:
    header = struct.pack("!HHHHHH", 0, 0, len(questions), 0, 0, 0)
    return header + b"".join(encode_name(n) + struct.pack("!HH", t, 1) for n, t in questions)


def _read_name(buf: bytes, off: int) -> tuple[str, int]:
    labels: list[str] = []
    end: int | None = None
    for _ in range(128):                       # loop guard against pointer cycles
        length = buf[off]
        if length & 0xC0 == 0xC0:
            ptr = ((length & 0x3F) << 8) | buf[off + 1]
            if end is None:
                end = off + 2
            off = ptr
            continue
        off += 1
        if length == 0:
            return ".".join(labels), (end if end is not None else off)
        labels.append(buf[off:off + length].decode(errors="replace"))
        off += length
    raise ValueError("name too long or compression loop")


def parse_records(buf: bytes) -> list[tuple[str, int, object]]:
    """Return (name, type, value) for every answer/authority/additional record."""
    _id, _flags, qd, an, ns, ar = struct.unpack_from("!HHHHHH", buf, 0)
    off = 12
    for _ in range(qd):
        _, off = _read_name(buf, off)
        off += 4
    records = []
    for _ in range(an + ns + ar):
        name, off = _read_name(buf, off)
        rtype, _cls, _ttl, rdlen = struct.unpack_from("!HHIH", buf, off)
        off += 10
        rdata_off, off = off, off + rdlen
        value: object = None
        if rtype == T_PTR:
            value, _ = _read_name(buf, rdata_off)
        elif rtype == T_SRV:
            _prio, _weight, port = struct.unpack_from("!HHH", buf, rdata_off)
            target, _ = _read_name(buf, rdata_off + 6)
            value = (target, port)
        elif rtype == T_A and rdlen == 4:
            value = socket.inet_ntoa(buf[rdata_off:rdata_off + 4])
        elif rtype == T_TXT:
            txt: dict[str, str] = {}
            p = rdata_off
            while p < rdata_off + rdlen:
                n = buf[p]
                entry = buf[p + 1:p + 1 + n].decode(errors="replace")
                p += 1 + n
                if entry:
                    k, _, v = entry.partition("=")
                    txt[k.lower()] = v
            value = txt
        if value is not None:
            records.append((name.lower(), rtype, value))
    return records


def assemble(records: list[tuple[str, int, object]]) -> dict[str, Service]:
    services: dict[str, Service] = {}
    srv: dict[str, tuple[str, int]] = {}
    txt: dict[str, dict[str, str]] = {}
    addrs: dict[str, list[str]] = {}
    for name, rtype, value in records:
        if rtype == T_PTR and name == SERVICE:
            services.setdefault(value.lower(), Service(instance=value))  # type: ignore[union-attr,arg-type]
        elif rtype == T_SRV:
            srv[name] = value                                     # type: ignore[assignment]
        elif rtype == T_TXT:
            txt.setdefault(name, {}).update(value)                # type: ignore[arg-type]
        elif rtype == T_A:
            lst = addrs.setdefault(name, [])
            if value not in lst:
                lst.append(value)                                 # type: ignore[arg-type]
    for inst, svc in services.items():
        if inst in srv:
            svc.target, svc.port = srv[inst][0].lower(), srv[inst][1]
            svc.addresses = addrs.get(svc.target, [])
        svc.txt = txt.get(inst, {})
    return services


# --------------------------------------------------------------------------- browsing

def discover(timeout: float = 2.0) -> list[Service]:
    records: list[tuple[str, int, object]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.bind(("", 0))

        def collect(query: bytes, seconds: float) -> None:
            sock.sendto(query, MDNS_ADDR)
            deadline = time.monotonic() + seconds
            while (left := deadline - time.monotonic()) > 0:
                sock.settimeout(left)
                try:
                    data, _ = sock.recvfrom(9000)
                except (socket.timeout, TimeoutError):
                    break
                try:
                    records.extend(parse_records(data))
                except (ValueError, IndexError, struct.error):
                    continue

        collect(build_query((SERVICE, T_PTR)), timeout * 0.6)
        # Responders don't always include SRV/TXT/A as additional records.
        services = assemble(records)
        missing = [(s.instance, t) for s in services.values() if s.target is None for t in (T_SRV, T_TXT)]
        if missing:
            collect(build_query(*missing), timeout * 0.2)
        services = assemble(records)
        targets = [(s.target, T_A) for s in services.values() if s.target and not s.addresses]
        if targets:
            collect(build_query(*targets), timeout * 0.2)
    except OSError:
        return []
    finally:
        sock.close()
    return sorted(assemble(records).values(), key=lambda s: s.instance)


def find_light(timeout: float = 2.0) -> Service | None:
    """First usable light, or None. Raises if only mTLS (tls=) devices exist."""
    services = discover(timeout)
    usable = [s for s in services if s.address and s.port and not s.uses_tls]
    if usable:
        return usable[0]
    tls = [s for s in services if s.uses_tls]
    if tls:
        raise UnsupportedDeviceError(
            f"{tls[0].instance} advertises tls= in its mDNS record: it uses Elgato's mTLS "
            "protocol, which this tool does not support")
    return None
