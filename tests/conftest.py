import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from matador.kalshi.auth import KalshiSigner


@pytest.fixture
def rsa_keypair(tmp_path):
    """A throwaway RSA keypair for signer tests -- never a real Kalshi credential."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "throwaway_key.pem"
    key_path.write_bytes(pem)
    return key_path, private_key.public_key()


@pytest.fixture
def signer(rsa_keypair):
    key_path, _ = rsa_keypair
    return KalshiSigner(key_id="test-key-id", private_key_path=str(key_path))
