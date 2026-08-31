from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _generate_test_keypair() -> tuple[bytes, bytes]:
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
    return private_pem, public_pem


# --- Environment must be configured before `app.main` (and therefore
# `app.config.get_settings()`) is imported anywhere, since Settings is
# constructed once at module import time. conftest.py is collected before
# any test module in this directory, so this runs first. ---

_PRIVATE_PEM, _PUBLIC_PEM = _generate_test_keypair()
_REDIS_PORT = _free_port()
_UPSTREAM_PORT = _free_port()

os.environ["GATEWAY_JWT_PUBLIC_KEY"] = _PUBLIC_PEM.decode("utf-8")
os.environ["GATEWAY_JWT_ALGORITHM"] = "RS256"
os.environ["GATEWAY_REDIS_URL"] = f"redis://127.0.0.1:{_REDIS_PORT}/0"
os.environ["GATEWAY_UPSTREAM_BASE_URL"] = f"http://127.0.0.1:{_UPSTREAM_PORT}"
os.environ["GATEWAY_UPSTREAM_TIMEOUT_SECONDS"] = "10"
os.environ["GATEWAY_UPSTREAM_CONNECT_TIMEOUT_SECONDS"] = "5"
os.environ["GATEWAY_LANGFUSE_ENABLED"] = "false"
os.environ["GATEWAY_SEMANTIC_CACHE_ENABLED"] = "true"
import tempfile
_QDRANT_CACHE_DIR = tempfile.mkdtemp(prefix="gateway_pytest_qdrant_")
os.environ["GATEWAY_SEMANTIC_CACHE_PATH"] = _QDRANT_CACHE_DIR
os.environ["GATEWAY_PERMISSIONS_FILE_PATH"] = str(_REPO_ROOT / "app" / "policy" / "permissions.yaml")
os.environ["GATEWAY_RATE_LIMIT_MAX_REQUESTS"] = "1000"


@pytest.fixture(scope="session", autouse=True)
def _fake_redis_server():
    from fakeredis import TcpFakeServer

    server = TcpFakeServer(("127.0.0.1", _REDIS_PORT), server_type="redis")
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    yield server
    server.shutdown()


@pytest.fixture(scope="session", autouse=True)
def _fake_upstream_server():
    from tests.fake_upstream import FakeUpstreamServer

    server = FakeUpstreamServer(host="127.0.0.1", port=_UPSTREAM_PORT)
    server.start()
    time.sleep(0.1)
    yield server
    server.stop()


@pytest.fixture()
def upstream(_fake_upstream_server):
    return _fake_upstream_server


@pytest.fixture()
def mint_token():
    import time as _time

    import jwt

    def _mint(*, user_id: str, department: str, clearance: str, ttl: int = 3600) -> str:
        now = int(_time.time())
        claims = {
            "sub": user_id,
            "department": department,
            "clearance_level": clearance,
            "iat": now,
            "exp": now + ttl,
        }
        return jwt.encode(claims, _PRIVATE_PEM, algorithm="RS256")

    return _mint


@pytest.fixture(scope="session")
def client(_fake_redis_server, _fake_upstream_server):
    from fastapi.testclient import TestClient

    import app.main as main_module

    with TestClient(main_module.app) as test_client:
        yield test_client
