"""This computer on the home network: its name, its private addresses, and which clients count as home.

The interface opens to the home network only: it listens on this computer's private address, and answers
clients whose addresses are in the private ranges (RFC 1918, link-local) or on this computer.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass

PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8")
)
"""The address ranges of a home network (and of this computer)."""


def is_home(address) -> bool:
    """True if ``address`` (text or an address) is in one of the private ranges."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in network for network in PRIVATE_NETWORKS if network.version == ip.version)


def hostname() -> str:
    """This computer's name, as certificates and ``.local`` names write it (letters, digits, hyphens)."""
    name = socket.gethostname().split(".")[0].lower()
    return re.sub(r"[^a-z0-9-]", "", name).strip("-")


PREFERENCE = (ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("10.0.0.0/8"))
"""Home routers mostly hand out 192.168 addresses; VPNs often use 10/8, so those come later."""


def _rank(ip: ipaddress.IPv4Address) -> int:
    return next((i for i, network in enumerate(PREFERENCE) if ip in network), len(PREFERENCE))


@dataclass(frozen=True)
class Lan:
    """The interface's home-network address while it is open."""

    address: str
    port: int
    names: tuple[str, ...] = ()
    """This computer's names (``name``, ``name.local``) that phones may use instead of the address."""

    @property
    def url(self) -> str:
        return f"https://{self.address}:{self.port}"

    @property
    def hosts(self) -> set[str]:
        return {f"{host}:{self.port}" for host in (self.address, *self.names)}

    @property
    def origins(self) -> set[str]:
        return {f"https://{host}" for host in self.hosts}


def addresses() -> list[ipaddress.IPv4Address]:
    """This computer's home-network addresses: 192.168 ones first, then the one the system routes through."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1; connecting a UDP socket sends nothing, it only picks the route
            found.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        found += [str(info[4][0]) for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    out: list[ipaddress.IPv4Address] = []
    for text in found:
        ip = ipaddress.ip_address(text)
        if isinstance(ip, ipaddress.IPv4Address) and is_home(ip) and not ip.is_loopback and ip not in out:
            out.append(ip)
    return sorted(out, key=_rank)  # stable: within a rank, the routed one stays first
