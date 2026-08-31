from __future__ import annotations

import argparse
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_KEY_DIR = Path(__file__).resolve().parent.parent / ".dev_keys"
_PRIVATE_KEY_PATH = _KEY_DIR / "rsa_private.pem"
_PUBLIC_KEY_PATH = _KEY_DIR / "rsa_public.pem"


def _load_or_create_keypair() -> tuple[bytes, bytes]:
    _KEY_DIR.mkdir(exist_ok=True)
    if _PRIVATE_KEY_PATH.exists() and _PUBLIC_KEY_PATH.exists():
        return _PRIVATE_KEY_PATH.read_bytes(), _PUBLIC_KEY_PATH.read_bytes()

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _PRIVATE_KEY_PATH.write_bytes(private_pem)
    _PUBLIC_KEY_PATH.write_bytes(public_pem)
    print(f"[generate_mock_jwt] Generated a new throwaway dev keypair under {_KEY_DIR}/")
    print(
        "[generate_mock_jwt] Put this in your .env as GATEWAY_JWT_PUBLIC_KEY "
        "(or export it) so the gateway can verify tokens minted by this script:\n"
    )
    print(public_pem.decode("utf-8"))
    return private_pem, public_pem


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", required=True, help="Value for the 'sub' claim.")
    parser.add_argument("--department", required=True, help="e.g. hr, eng, finance.")
    parser.add_argument(
        "--clearance", required=True, choices=["junior", "mid", "senior", "admin"], help="Clearance level."
    )
    parser.add_argument("--email", default=None)
    parser.add_argument("--ttl", type=int, default=3600, help="Token lifetime in seconds (default: 1 hour).")
    args = parser.parse_args()

    private_pem, _ = _load_or_create_keypair()

    now = int(time.time())
    claims = {
        "sub": args.user_id,
        "department": args.department,
        "clearance_level": args.clearance,
        "iat": now,
        "exp": now + args.ttl,
    }
    if args.email:
        claims["email"] = args.email

    token = jwt.encode(claims, private_pem, algorithm="RS256")
    print(token)


if __name__ == "__main__":
    main()
