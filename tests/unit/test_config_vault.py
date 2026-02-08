"""Tests for vault integration in src.config — VaultSettingsSource."""

from __future__ import annotations

import os

import pytest

from src.config import Settings
from src.utils.vault import SecretVault


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure no env vars leak from other test modules.

    Many test files set BOT_PRIVATE_KEY via os.environ at module level,
    which persists for the entire session and overrides dotenv values.
    """
    monkeypatch.delenv("VAULT_PASSWORD", raising=False)
    monkeypatch.delenv("VAULT_PATH", raising=False)
    monkeypatch.delenv("BOT_ENV_FILE", raising=False)
    monkeypatch.delenv("BOT_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("BOT_FUNDER", raising=False)
    monkeypatch.delenv("BOT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("BOT_DASHBOARD_PASSWORD", raising=False)


class TestVaultSettingsSource:
    """Verify Settings loads secrets from vault when available."""

    def test_loads_from_vault(self, tmp_path, monkeypatch):
        """Settings should prefer vault secrets over .env."""
        vault_path = tmp_path / "secrets.vault"
        env_path = tmp_path / ".env"

        # Write .env with a placeholder key
        env_path.write_text("BOT_PRIVATE_KEY=from_env\n")

        # Create vault with a different key
        vault = SecretVault(vault_path, "testpw")
        vault.create({"private_key": "from_vault"})

        monkeypatch.setenv("VAULT_PASSWORD", "testpw")
        monkeypatch.setenv("VAULT_PATH", str(vault_path))
        monkeypatch.chdir(tmp_path)

        settings = Settings(_env_file=str(env_path))  # type: ignore[call-arg]
        assert settings.private_key.get_secret_value() == "from_vault"

    def test_falls_back_to_env(self, tmp_path, monkeypatch):
        """Without vault, Settings should load from .env as before."""
        env_path = tmp_path / ".env"
        env_path.write_text("BOT_PRIVATE_KEY=from_env_only\n")

        # No vault password = vault disabled
        monkeypatch.delenv("VAULT_PASSWORD", raising=False)
        monkeypatch.chdir(tmp_path)

        settings = Settings(_env_file=str(env_path))  # type: ignore[call-arg]
        assert settings.private_key.get_secret_value() == "from_env_only"

    def test_vault_missing_file(self, tmp_path, monkeypatch):
        """Vault password set but file doesn't exist — fallback to .env."""
        env_path = tmp_path / ".env"
        env_path.write_text("BOT_PRIVATE_KEY=from_env_fallback\n")

        monkeypatch.setenv("VAULT_PASSWORD", "pw")
        monkeypatch.setenv("VAULT_PATH", str(tmp_path / "nonexistent.vault"))
        monkeypatch.chdir(tmp_path)

        settings = Settings(_env_file=str(env_path))  # type: ignore[call-arg]
        assert settings.private_key.get_secret_value() == "from_env_fallback"

    def test_vault_wrong_password_falls_back(self, tmp_path, monkeypatch):
        """Wrong vault password — should fall back to .env gracefully."""
        vault_path = tmp_path / "secrets.vault"
        env_path = tmp_path / ".env"
        env_path.write_text("BOT_PRIVATE_KEY=env_key\n")

        vault = SecretVault(vault_path, "correct_pw")
        vault.create({"private_key": "vault_key"})

        monkeypatch.setenv("VAULT_PASSWORD", "wrong_pw")
        monkeypatch.setenv("VAULT_PATH", str(vault_path))
        monkeypatch.chdir(tmp_path)

        settings = Settings(_env_file=str(env_path))  # type: ignore[call-arg]
        assert settings.private_key.get_secret_value() == "env_key"
