"""Validate every destination and return numeric addresses for pinned dialing."""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .results import CrawlError


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    addresses: tuple[str, ...]
    scheme: str


def public_address(value: str) -> bool:
    address = ipaddress.ip_address(value.split("%")[0])
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif address.sixtofour or address.teredo:
            return False  # Transition tunnels can encode a second, private destination.
    return address.is_global and not (address.is_multicast or address.is_reserved or address.is_unspecified)


def resolve_target(url: str, protection: bool = True) -> Target:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Invalid HTTP target")
        if "%" in host or any(ord(char) <= 32 for char in host):
            raise ValueError("Invalid hostname")
        host = host.encode("idna").decode("ascii").rstrip(".").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = tuple(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        if not addresses or (protection and not all(public_address(ip) for ip in addresses)):
            raise CrawlError("Target blocked by network policy", 400)
        return Target(host, port, addresses, parsed.scheme)
    except CrawlError:
        raise
    except (ValueError, UnicodeError, OSError) as exc:
        raise CrawlError("Invalid or unresolvable HTTP target", 400) from exc
