from capture.parse import anonymize_ip, byte_entropy, is_http_request, tls_sni_length


def client_hello(host: bytes) -> bytes:
    sni = (b"\x00\x00" + (len(host) + 5).to_bytes(2, "big")
           + (len(host) + 3).to_bytes(2, "big") + b"\x00"
           + len(host).to_bytes(2, "big") + host)
    body = (b"\x03\x03" + b"\x00" * 32 + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00"
            + len(sni).to_bytes(2, "big") + sni)
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs


def test_sni_length_extracted():
    assert tls_sni_length(client_hello(b"example.com")) == 11
    assert tls_sni_length(client_hello(b"a.very.long.hostname.example.org")) == 32


def test_sni_returns_zero_for_non_tls():
    for payload in (b"", b"GET / HTTP/1.1\r\n\r\n", b"\x16\x03\x01\x00\x05hello", b"\x00" * 60):
        assert tls_sni_length(payload) == 0


def test_sni_survives_truncated_handshake():
    truncated = client_hello(b"example.com")[:30]
    assert tls_sni_length(truncated) == 0


def test_http_request_detection():
    assert is_http_request(b"GET /index.html HTTP/1.1\r\n")
    assert is_http_request(b"POST /api HTTP/1.1\r\n")
    assert not is_http_request(b"HTTP/1.1 200 OK\r\n")   # a response, not a request
    assert not is_http_request(b"\x16\x03\x01")
    assert not is_http_request(b"")


def test_byte_entropy_bounds():
    assert byte_entropy(b"") == 0.0
    assert byte_entropy(b"aaaaaaaa") == 0.0
    assert byte_entropy(bytes(range(256))) == 8.0


def test_anonymize_ip_is_stable_and_not_reversible():
    a = anonymize_ip("192.168.1.10", "salt")
    assert a == anonymize_ip("192.168.1.10", "salt")
    assert a != anonymize_ip("192.168.1.11", "salt")
    assert a != anonymize_ip("192.168.1.10", "other-salt")
    assert "192.168" not in a
    assert anonymize_ip("2001:db8::1", "salt").startswith("100::")
