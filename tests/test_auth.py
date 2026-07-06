import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

_PSS = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)


def _verify(public_key, signature_b64: str, message: bytes) -> None:
    public_key.verify(base64.b64decode(signature_b64), message, _PSS, hashes.SHA256())


def test_sign_uses_exact_message_format_and_pss_sha256(signer, rsa_keypair):
    _, public_key = rsa_keypair
    ts, method, path = "1700000000000", "GET", "/trade-api/v2/markets"

    signature = signer.sign(ts, method, path)

    _verify(public_key, signature, f"{ts}{method}{path}".encode("utf-8"))


def test_sign_rejects_tampered_message(signer, rsa_keypair):
    _, public_key = rsa_keypair
    signature = signer.sign("1700000000000", "GET", "/trade-api/v2/markets")

    with pytest.raises(InvalidSignature):
        _verify(public_key, signature, b"1700000000000GET/trade-api/v2/orderbook")


def test_headers_has_exactly_the_three_kalshi_fields(signer):
    headers = signer.headers("GET", "/trade-api/v2/markets", now_ms=1700000000000)

    assert set(headers) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"


def test_headers_signature_verifies_against_the_signed_request(signer, rsa_keypair):
    _, public_key = rsa_keypair
    headers = signer.headers("get", "/trade-api/v2/markets/KXATPMATCH-26JUL02/orderbook", now_ms=1700000000000)

    expected_message = b"1700000000000GET/trade-api/v2/markets/KXATPMATCH-26JUL02/orderbook"
    _verify(public_key, headers["KALSHI-ACCESS-SIGNATURE"], expected_message)


def test_headers_uses_current_time_when_not_given(signer):
    import time

    before = int(time.time() * 1000)
    headers = signer.headers("GET", "/trade-api/v2/markets")
    after = int(time.time() * 1000)

    assert before <= int(headers["KALSHI-ACCESS-TIMESTAMP"]) <= after
