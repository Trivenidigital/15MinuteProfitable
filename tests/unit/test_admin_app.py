"""Tests for src.admin.app — admin dashboard REST API."""

from __future__ import annotations

import base64
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.admin.app import create_admin_app
from src.utils.vault import SecretVault


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch, tmp_path):
    """Set up admin env vars for every test."""
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "testpass")
    monkeypatch.setenv("VAULT_PASSWORD", "vaultpw")
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "secrets.vault"))
    monkeypatch.setenv("BOT_ENV_FILE", str(tmp_path / ".env"))

    # Pre-register BOT_ keys that the save_settings endpoint may write to
    # os.environ directly (bypassing monkeypatch).  By calling delenv here,
    # monkeypatch records the original state and restores it after the test,
    # preventing env pollution into subsequent test modules.
    monkeypatch.delenv("BOT_DRY_RUN", raising=False)
    monkeypatch.delenv("BOT_ORDER_SIZE", raising=False)

    # Create a minimal .env so Settings can load
    env_path = tmp_path / ".env"
    env_path.write_text("BOT_PRIVATE_KEY=0xtest123\n")


@pytest.fixture()
def client():
    app = create_admin_app()
    return TestClient(app)


@pytest.fixture()
def auth_headers():
    creds = base64.b64encode(b"admin:testpass").decode()
    return {"Authorization": f"Basic {creds}"}


@pytest.fixture()
def bad_auth_headers():
    creds = base64.b64encode(b"admin:wrong").decode()
    return {"Authorization": f"Basic {creds}"}


class TestAuth:
    def test_no_auth_returns_401(self, client):
        resp = client.get("/")
        assert resp.status_code == 401

    def test_bad_auth_returns_401(self, client, bad_auth_headers):
        resp = client.get("/", headers=bad_auth_headers)
        assert resp.status_code == 401

    def test_good_auth_returns_200(self, client, auth_headers):
        resp = client.get("/", headers=auth_headers)
        assert resp.status_code == 200


class TestPages:
    def test_settings_page(self, client, auth_headers):
        resp = client.get("/", headers=auth_headers)
        assert resp.status_code == 200
        assert "Settings" in resp.text

    def test_wallet_page(self, client, auth_headers):
        resp = client.get("/wallet", headers=auth_headers)
        assert resp.status_code == 200
        assert "Wallet" in resp.text or "Vault" in resp.text

    def test_control_page(self, client, auth_headers):
        resp = client.get("/control", headers=auth_headers)
        assert resp.status_code == 200
        assert "Bot" in resp.text


class TestSettingsAPI:
    def test_save_settings(self, client, auth_headers, tmp_path):
        resp = client.post(
            "/api/settings",
            json={"BOT_ORDER_SIZE": "500", "BOT_DRY_RUN": "true"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["updated"] == 2

    def test_save_settings_filters_secrets(self, client, auth_headers):
        resp = client.post(
            "/api/settings",
            json={"BOT_PRIVATE_KEY": "should_be_filtered", "BOT_ORDER_SIZE": "100"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        # Only non-secret key should be written
        assert resp.json()["updated"] == 1


class TestSecretsAPI:
    def test_save_secrets_creates_vault(self, client, auth_headers, tmp_path):
        resp = client.post(
            "/api/secrets",
            json={"private_key": "0xnewkey", "telegram_bot_token": "tok123"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Verify vault was created
        vault_path = tmp_path / "secrets.vault"
        assert vault_path.is_file()

        vault = SecretVault(vault_path, "vaultpw")
        data = vault.load()
        assert data["private_key"] == "0xnewkey"
        assert data["telegram_bot_token"] == "tok123"

    def test_save_secrets_no_vault_password(self, client, auth_headers, monkeypatch):
        monkeypatch.setenv("VAULT_PASSWORD", "")
        # Recreate client to pick up new env
        app = create_admin_app()
        new_client = TestClient(app)

        resp = new_client.post(
            "/api/secrets",
            json={"private_key": "0x123"},
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestVaultAPI:
    def test_vault_status_no_vault(self, client, auth_headers):
        resp = client.get("/api/vault/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["exists"] is False

    def test_vault_init(self, client, auth_headers, tmp_path):
        resp = client.post("/api/vault/init", headers=auth_headers)
        assert resp.status_code == 200

        # Check vault exists now
        resp = client.get("/api/vault/status", headers=auth_headers)
        data = resp.json()
        assert data["exists"] is True
        assert data["unlocked"] is True
        assert data["key_count"] == 0

    def test_vault_init_duplicate(self, client, auth_headers):
        client.post("/api/vault/init", headers=auth_headers)
        resp = client.post("/api/vault/init", headers=auth_headers)
        assert resp.status_code == 409

    def test_vault_change_password(self, client, auth_headers, tmp_path):
        # Init vault first
        client.post("/api/vault/init", headers=auth_headers)
        # Add a secret
        client.post(
            "/api/secrets",
            json={"test_key": "test_value"},
            headers=auth_headers,
        )

        # Change password
        resp = client.post(
            "/api/vault/change-password",
            json={"new_password": "newpw"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Old password should fail
        vault_path = tmp_path / "secrets.vault"
        from src.utils.vault import VaultError

        old_vault = SecretVault(vault_path, "vaultpw")
        with pytest.raises(VaultError):
            old_vault.load()

        # New password should work
        new_vault = SecretVault(vault_path, "newpw")
        data = new_vault.load()
        assert data["test_key"] == "test_value"


class TestWalletDerive:
    """Tests for POST /api/wallet/derive endpoint."""

    # Hardhat default account #0
    _TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    _TEST_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

    def test_derive_address_from_private_key(self, client, auth_headers):
        resp = client.post(
            "/api/wallet/derive",
            json={"private_key": self._TEST_KEY},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["eth_address"].lower() == self._TEST_ADDRESS.lower()

    def test_derive_signature_type_eoa(self, client, auth_headers):
        resp = client.post(
            "/api/wallet/derive",
            json={"private_key": self._TEST_KEY},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["signature_type"] == 0

    def test_derive_signature_type_poly_gnosis_safe(self, client, auth_headers):
        funder = "0x1234567890abcdef1234567890abcdef12345678"
        resp = client.post(
            "/api/wallet/derive",
            json={"private_key": self._TEST_KEY, "polymarket_address": funder},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["signature_type"] == 1
        assert data["funder"] == funder

    def test_derive_invalid_private_key(self, client, auth_headers):
        resp = client.post(
            "/api/wallet/derive",
            json={"private_key": "not-a-valid-key"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "Invalid private key" in resp.json()["detail"]

    def test_derive_missing_private_key(self, client, auth_headers):
        resp = client.post(
            "/api/wallet/derive",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "private_key is required" in resp.json()["detail"]

    def test_derive_requires_auth(self, client):
        resp = client.post(
            "/api/wallet/derive",
            json={"private_key": self._TEST_KEY},
        )
        assert resp.status_code == 401

    def test_derive_api_verification_mocked(self, client, auth_headers):
        mock_creds = {"apiKey": "abc12345deadbeef", "secret": "s", "passphrase": "p"}
        mock_clob = MagicMock()
        mock_clob.derive_api_key.return_value = mock_creds
        with patch("src.admin.app._make_clob_client", return_value=mock_clob):
            resp = client.post(
                "/api/wallet/derive",
                json={"private_key": self._TEST_KEY},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["api_verified"] is True
        assert data["api_key_preview"].startswith("abc12345dead")

    def test_derive_api_verification_fails_gracefully(self, client, auth_headers):
        mock_clob = MagicMock()
        mock_clob.derive_api_key.side_effect = RuntimeError("connection refused")
        with patch("src.admin.app._make_clob_client", return_value=mock_clob):
            resp = client.post(
                "/api/wallet/derive",
                json={"private_key": self._TEST_KEY},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        # Address should still be derived even if API verification fails
        assert data["eth_address"].lower() == self._TEST_ADDRESS.lower()
        assert data["api_verified"] is False
        assert "connection refused" in data["api_error"]


class TestBotControlAPI:
    """Bot control tests — these call systemctl which may not be available."""

    def test_bot_status(self, client, auth_headers):
        resp = client.get("/api/bot/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        # On non-Linux systems, status will be "unknown"
        assert "status" in data
