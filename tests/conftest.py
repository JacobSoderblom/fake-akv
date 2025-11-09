import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import requests
import uvicorn

# Make sure "src" is on sys.path so "fake_akv" imports work when running pytest from project root
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from datetime import datetime, timedelta, timezone  # noqa: E402

# --- Self-signed certificate generation (no OpenSSL dependency) ---
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

# Import the FastAPI app AFTER sys.path fix
from fake_akv.main import app  # noqa: E402


def _make_self_signed_cert(
    tmpdir: Path, common_name: str = "localhost"
) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    san = x509.SubjectAlternativeName(
        [
            x509.DNSName("localhost"),
            x509.DNSName("127.0.0.1"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )

    key_path = tmpdir / "dev.key"
    crt_path = tmpdir / "dev.crt"
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(crt_path), str(key_path)


@pytest.fixture(scope="session", autouse=True)
def run_https_server():
    """
    Start FastAPI with HTTPS on 127.0.0.1:8443 for the whole test session.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="fake_akv_tls_"))
    certfile, keyfile = _make_self_signed_cert(tmpdir, common_name="localhost")

    # Start uvicorn server in a background thread
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=8443,
        log_level="warning",
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()

    # Poll for readiness over HTTPS (ignore cert validation)
    base_url = "https://127.0.0.1:8443"
    deadline = time.time() + 10  # up to 10s to boot
    last_err = None
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/?api-version=7.4", verify=False, timeout=0.5)
            if r.status_code == 200:
                break
        except Exception as e:
            last_err = e
        time.sleep(0.2)
    else:
        # if we never broke out of the loop
        raise RuntimeError(f"Server failed to start: {last_err}")

    # expose URL to tests via env (optional convenience)
    os.environ.setdefault("FAKE_AKV_BASE_URL", base_url)

    yield  # run tests

    server.should_exit = True
    thread.join(timeout=3)
