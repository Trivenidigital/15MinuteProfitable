"""Standalone entry point for the admin dashboard.

Usage::

    # Required env vars:
    #   ADMIN_USERNAME, ADMIN_PASSWORD, VAULT_PASSWORD

    python -m src.admin

    # With custom port:
    ADMIN_PORT=9443 python -m src.admin
"""

from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path

import uvicorn

from src.admin.app import create_admin_app
from src.admin.tls import ensure_tls_cert


def main() -> None:
    host = os.environ.get("ADMIN_HOST", "0.0.0.0")
    port = int(os.environ.get("ADMIN_PORT", "8443"))

    # Validate required env vars
    if not os.environ.get("ADMIN_USERNAME") or not os.environ.get("ADMIN_PASSWORD"):
        print(
            "Error: ADMIN_USERNAME and ADMIN_PASSWORD environment variables are required.",
            file=sys.stderr,
        )
        sys.exit(1)

    # TLS cert setup
    cert_path_env = os.environ.get("ADMIN_TLS_CERT")
    key_path_env = os.environ.get("ADMIN_TLS_KEY")

    cert_path, key_path = ensure_tls_cert(
        cert_path=Path(cert_path_env) if cert_path_env else None,
        key_path=Path(key_path_env) if key_path_env else None,
    )

    print(f"Admin dashboard starting on https://{host}:{port}")
    print(f"  TLS cert: {cert_path}")
    print(f"  TLS key:  {key_path}")

    app = create_admin_app()

    uvicorn.run(
        app,
        host=host,
        port=port,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
        log_level="info",
    )


if __name__ == "__main__":
    main()
