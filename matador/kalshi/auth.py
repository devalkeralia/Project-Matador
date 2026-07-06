import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


class KalshiSigner:
    """RSA-PSS/SHA-256 request signer for Kalshi's KALSHI-ACCESS-* auth headers."""

    def __init__(self, key_id: str, private_key_path: str):
        self.key_id = key_id
        key_bytes = Path(private_key_path).read_bytes()
        self._private_key = serialization.load_pem_private_key(key_bytes, password=None)

    def sign(self, timestamp_ms: str, method: str, signing_path: str) -> str:
        # signing_path must include /trade-api/v2 and exclude the query string.
        message = f"{timestamp_ms}{method}{signing_path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def headers(self, method: str, signing_path: str, now_ms: int | None = None) -> dict[str, str]:
        timestamp_ms = str(now_ms if now_ms is not None else int(time.time() * 1000))
        signature = self.sign(timestamp_ms, method.upper(), signing_path)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }
