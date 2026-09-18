"""Packet parsing helpers, kept separate from the capture loop so they can be
tested on raw bytes without scapy, libpcap or elevated privileges."""
from __future__ import annotations

_HTTP_METHODS = (b"GET ", b"POST ", b"PUT ", b"HEAD ", b"DELETE ", b"OPTIONS ",
                 b"PATCH ", b"CONNECT ", b"TRACE ")


def is_http_request(payload: bytes) -> bool:
    return payload.startswith(_HTTP_METHODS)


def tls_sni_length(payload: bytes) -> int:
    """Length of the SNI host name in a TLS ClientHello, or 0.

    Only the handshake *metadata* is read — the length of the requested name,
    never the name itself and never application data.  Returns 0 for anything
    that is not a well-formed ClientHello, which is the common case and must
    stay cheap.
    """
    try:
        if len(payload) < 45 or payload[0] != 0x16:  # handshake record
            return 0
        if payload[5] != 0x01:  # ClientHello
            return 0
        pos = 43                                  # past version + random
        session_len = payload[pos]
        pos += 1 + session_len
        cipher_len = int.from_bytes(payload[pos:pos + 2], "big")
        pos += 2 + cipher_len
        comp_len = payload[pos]
        pos += 1 + comp_len
        if pos + 2 > len(payload):
            return 0
        ext_total = int.from_bytes(payload[pos:pos + 2], "big")
        pos += 2
        end = min(pos + ext_total, len(payload))
        while pos + 4 <= end:
            ext_type = int.from_bytes(payload[pos:pos + 2], "big")
            ext_len = int.from_bytes(payload[pos + 2:pos + 4], "big")
            body = pos + 4
            if ext_type == 0x0000:  # server_name
                if body + 5 > len(payload):
                    return 0
                return int.from_bytes(payload[body + 3:body + 5], "big")
            pos = body + ext_len
    except (IndexError, ValueError):
        return 0
    return 0


def byte_entropy(payload: bytes) -> float:
    """Shannon entropy of payload bytes.

    Only used when payload inspection is explicitly enabled
    (``CN_STORE_PAYLOADS=1``); the default feature set uses packet-size entropy
    instead and never looks at payload content.
    """
    if not payload:
        return 0.0
    import math

    counts = [0] * 256
    for b in payload:
        counts[b] += 1
    n = len(payload)
    acc = 0.0
    for c in counts:
        if c:
            p = c / n
            acc -= p * math.log2(p)
    return acc


def anonymize_ip(ip: str, salt: str) -> str:
    """Stable pseudonym for an address: keeps correlation, drops identity."""
    import hashlib

    digest = hashlib.blake2b(f"{salt}|{ip}".encode(), digest_size=4).digest()
    if ":" in ip:
        return "100::" + ":".join(f"{digest[i]:02x}{digest[i + 1]:02x}" for i in (0, 2))
    return "100.64." + ".".join(str(b) for b in digest[2:4])
