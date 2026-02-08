"""Tests for src.utils.vault — AES-256-GCM encrypted secrets vault."""

from __future__ import annotations

import json

import pytest

from src.utils.vault import SecretVault, VaultError


@pytest.fixture()
def vault_path(tmp_path):
    return tmp_path / "secrets.vault"


class TestSecretVault:
    """Core vault create/load/save lifecycle."""

    def test_create_and_load(self, vault_path):
        vault = SecretVault(vault_path, password="testpass")
        secrets = {"private_key": "0xabc123", "token": "tok_xyz"}

        vault.create(secrets)
        assert vault.exists()

        loaded = vault.load()
        assert loaded == secrets

    def test_create_rejects_duplicate(self, vault_path):
        vault = SecretVault(vault_path, password="pw")
        vault.create({"a": "1"})

        with pytest.raises(VaultError, match="already exists"):
            vault.create({"b": "2"})

    def test_load_nonexistent_raises(self, vault_path):
        vault = SecretVault(vault_path, password="pw")
        with pytest.raises(VaultError, match="not found"):
            vault.load()

    def test_wrong_password_raises(self, vault_path):
        vault_good = SecretVault(vault_path, password="correct")
        vault_good.create({"key": "value"})

        vault_bad = SecretVault(vault_path, password="wrong")
        with pytest.raises(VaultError, match="wrong password"):
            vault_bad.load()

    def test_get_set_delete(self, vault_path):
        vault = SecretVault(vault_path, password="pw")
        vault.create({"a": "1", "b": "2"})

        assert vault.get("a") == "1"
        assert vault.get("missing", "default") == "default"

        vault.set("c", "3")
        assert vault.get("c") == "3"

        vault.delete("a")
        assert vault.get("a") == ""
        assert vault.list_keys() == ["b", "c"]

    def test_list_keys(self, vault_path):
        vault = SecretVault(vault_path, password="pw")
        vault.create({"zebra": "z", "alpha": "a", "mid": "m"})

        assert vault.list_keys() == ["alpha", "mid", "zebra"]

    def test_save_overwrites(self, vault_path):
        vault = SecretVault(vault_path, password="pw")
        vault.create({"old": "data"})
        vault.save({"new": "data"})

        loaded = vault.load()
        assert loaded == {"new": "data"}

    def test_change_password(self, vault_path):
        vault = SecretVault(vault_path, password="old_pw")
        vault.create({"key": "value"})

        vault.change_password("new_pw")

        # Old password should fail
        vault_old = SecretVault(vault_path, password="old_pw")
        with pytest.raises(VaultError):
            vault_old.load()

        # New password should work
        vault_new = SecretVault(vault_path, password="new_pw")
        assert vault_new.load() == {"key": "value"}

    def test_empty_vault(self, vault_path):
        vault = SecretVault(vault_path, password="pw")
        vault.create({})

        assert vault.load() == {}
        assert vault.list_keys() == []

    def test_vault_file_format(self, vault_path):
        """Verify the vault file is proper JSON with expected envelope fields."""
        vault = SecretVault(vault_path, password="pw")
        vault.create({"test": "data"})

        raw = json.loads(vault_path.read_text())
        assert raw["version"] == 1
        assert raw["kdf"] == "pbkdf2-sha256"
        assert raw["kdf_iterations"] == 100_000
        assert "salt" in raw
        assert "nonce" in raw
        assert "ciphertext" in raw

    def test_corrupt_file_raises(self, vault_path):
        vault_path.parent.mkdir(parents=True, exist_ok=True)
        vault_path.write_text("not json at all")

        vault = SecretVault(vault_path, password="pw")
        with pytest.raises(VaultError, match="Corrupt"):
            vault.load()

    def test_caching(self, vault_path):
        """Load returns cached data without re-reading disk."""
        vault = SecretVault(vault_path, password="pw")
        vault.create({"key": "val"})

        # First load reads from disk
        data1 = vault.load()
        # Corrupt file on disk
        vault_path.write_text("corrupted")
        # Second load should return cached data
        data2 = vault.load()
        assert data1 == data2 == {"key": "val"}
