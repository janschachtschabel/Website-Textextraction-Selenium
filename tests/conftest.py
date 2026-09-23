import ipaddress
import socket

import pytest


@pytest.fixture
def dns(monkeypatch):
    """Answer fixture hostnames from the returned records, one address or a list of them.
    Numeric hosts resolve as usual; any other name fails as a missing record would."""
    records = {}
    resolve = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if host in records:
            answers = records[host] if isinstance(records[host], list) else [records[host]]
            return [
                (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
                for address in answers
            ]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise socket.gaierror(socket.EAI_NONAME, "No fixture DNS record") from None
        return resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return records


@pytest.fixture
def article_html():
    paragraphs = "".join(
        "<p>Light travels through transparent materials. Reflection changes its "
        "direction at a mirror. Students compare their observations and explain "
        "the relationship between the incident and reflected rays.</p>"
        for _ in range(8)
    )
    return (
        "<html><head><title>Optics</title></head><body><nav>NAVIGATIONTOKEN</nav>"
        "<main><h1>Optics</h1>" + paragraphs + "</main></body></html>"
    )
