"""Tests for src.admin.tls — self-signed TLS certificate generation."""

from __future__ import annotations

import pytest

from src.admin.tls import ensure_tls_cert


class TestEnsureTlsCert:

    def test_generates_cert_and_key(self, tmp_path):
        cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"

        result_cert, result_key = ensure_tls_cert(cert_path, key_path)

        assert result_cert == cert_path
        assert result_key == key_path
        assert cert_path.is_file()
        assert key_path.is_file()

        # Verify PEM format
        cert_data = cert_path.read_text()
        assert "BEGIN CERTIFICATE" in cert_data

        key_data = key_path.read_text()
        assert "BEGIN RSA PRIVATE KEY" in key_data

    def test_reuses_existing(self, tmp_path):
        cert_path = tmp_path / "cert.pem"
        key_path = tmp_path / "key.pem"

        # Generate first time
        ensure_tls_cert(cert_path, key_path)
        orig_cert = cert_path.read_bytes()

        # Call again — should NOT regenerate
        ensure_tls_cert(cert_path, key_path)
        assert cert_path.read_bytes() == orig_cert

    def test_creates_parent_directories(self, tmp_path):
        cert_path = tmp_path / "deep" / "nested" / "cert.pem"
        key_path = tmp_path / "deep" / "nested" / "key.pem"

        ensure_tls_cert(cert_path, key_path)
        assert cert_path.is_file()
        assert key_path.is_file()
