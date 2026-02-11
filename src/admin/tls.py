"""Self-signed TLS certificate generation for the admin dashboard.

Uses the ``cryptography`` library to generate a self-signed X.509 cert
with a 2048-bit RSA key, valid for 365 days.  If cert files already exist
they are reused.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


_DEFAULT_CERT_PATH = Path("data/admin-cert.pem")
_DEFAULT_KEY_PATH = Path("data/admin-key.pem")
_VALIDITY_DAYS = 365


def ensure_tls_cert(
    cert_path: Path | None = None,
    key_path: Path | None = None,
) -> tuple[Path, Path]:
    """Return *(cert_path, key_path)*, generating self-signed files if needed.

    Parameters
    ----------
    cert_path:
        Where to write / find the PEM certificate.  Defaults to
        ``data/admin-cert.pem``.
    key_path:
        Where to write / find the PEM private key.  Defaults to
        ``data/admin-key.pem``.

    Returns
    -------
    tuple[Path, Path]
        Resolved (cert_path, key_path).
    """
    cert_path = cert_path or _DEFAULT_CERT_PATH
    key_path = key_path or _DEFAULT_KEY_PATH

    if cert_path.is_file() and key_path.is_file():
        return cert_path, key_path

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    # Generate RSA key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Build self-signed certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "15MinuteProfitable Admin"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "15MinuteProfitable"),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=_VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(
                    __import__("ipaddress").IPv4Address("127.0.0.1")
                ),
                x509.IPAddress(
                    __import__("ipaddress").IPv4Address("0.0.0.0")
                ),
            ]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    # Write key
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    # Write cert
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    return cert_path, key_path
