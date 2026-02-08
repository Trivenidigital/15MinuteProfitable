"""AES-256-GCM encrypted secrets vault.

Stores sensitive configuration (private keys, API tokens) in an encrypted
file rather than plain-text .env.  The master password is derived into an
AES-256 key via PBKDF2-HMAC-SHA256 (100 000 iterations).

Vault file format (JSON envelope around ciphertext):
    {
        "version": 1,
        "kdf": "pbkdf2-sha256",
        "kdf_iterations": 100000,
        "salt": "<base64 32 bytes>",
        "nonce": "<base64 12 bytes>",
        "ciphertext": "<base64>"
    }
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

_KDF_ITERATIONS = 100_000
_SALT_BYTES = 32
_NONCE_BYTES = 12  # AES-GCM standard
_KEY_BYTES = 32  # AES-256
_VERSION = 1


def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from *password* and *salt* via PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text)


class VaultError(Exception):
    """Raised on vault I/O or crypto failures."""


class SecretVault:
    """Encrypted secrets vault backed by a single JSON file.

    Usage::

        vault = SecretVault(Path("data/secrets.vault"), password="changeme")
        if not vault.exists():
            vault.create({"private_key": "0xabc..."})
        secrets = vault.load()
    """

    def __init__(self, vault_path: Path, password: str) -> None:
        self._path = vault_path
        self._password = password
        self._cache: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def exists(self) -> bool:
        """Return True if the vault file exists on disk."""
        return self._path.is_file()

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, secrets: dict[str, str]) -> None:
        """Create a **new** vault file.  Raises if one already exists."""
        if self.exists():
            raise VaultError(f"Vault already exists at {self._path}")
        self._write(secrets)
        self._cache = dict(secrets)

    def load(self) -> dict[str, str]:
        """Decrypt and return the full secrets dict.  Caches in memory."""
        if self._cache is not None:
            return dict(self._cache)

        if not self.exists():
            raise VaultError(f"Vault file not found: {self._path}")

        raw = self._path.read_text(encoding="utf-8")
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VaultError(f"Corrupt vault file: {exc}") from exc

        version = envelope.get("version")
        if version != _VERSION:
            raise VaultError(f"Unsupported vault version: {version}")

        salt = _b64d(envelope["salt"])
        nonce = _b64d(envelope["nonce"])
        ciphertext = _b64d(envelope["ciphertext"])

        key = _derive_key(self._password, salt)
        aesgcm = AESGCM(key)

        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        except Exception as exc:
            raise VaultError("Decryption failed — wrong password?") from exc

        try:
            secrets: dict[str, str] = json.loads(plaintext.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise VaultError(f"Decrypted data is not valid JSON: {exc}") from exc

        self._cache = secrets
        return dict(secrets)

    def save(self, secrets: dict[str, str]) -> None:
        """Encrypt *secrets* and write atomically (tmp + rename)."""
        self._write(secrets)
        self._cache = dict(secrets)

    def get(self, key: str, default: str = "") -> str:
        """Return a single secret value."""
        data = self.load()
        return data.get(key, default)

    def set(self, key: str, value: str) -> None:
        """Set a single secret and persist."""
        data = self.load()
        data[key] = value
        self.save(data)

    def delete(self, key: str) -> None:
        """Remove a secret key and persist.  No-op if key missing."""
        data = self.load()
        data.pop(key, None)
        self.save(data)

    def list_keys(self) -> list[str]:
        """Return a sorted list of secret key names (no values)."""
        return sorted(self.load().keys())

    def change_password(self, new_password: str) -> None:
        """Re-encrypt the vault with a new master password."""
        data = self.load()
        self._password = new_password
        self._cache = None
        self._write(data)
        self._cache = dict(data)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write(self, secrets: dict[str, str]) -> None:
        """Encrypt and atomically write the vault file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        salt = os.urandom(_SALT_BYTES)
        nonce = os.urandom(_NONCE_BYTES)
        key = _derive_key(self._password, salt)

        aesgcm = AESGCM(key)
        plaintext = json.dumps(secrets, sort_keys=True).encode("utf-8")
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        envelope = {
            "version": _VERSION,
            "kdf": "pbkdf2-sha256",
            "kdf_iterations": _KDF_ITERATIONS,
            "salt": _b64e(salt),
            "nonce": _b64e(nonce),
            "ciphertext": _b64e(ciphertext),
        }

        # Atomic write: write to temp file in same directory, then rename
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._path.parent),
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(envelope, f, indent=2)
            # On Windows, need to remove target first
            if self._path.exists():
                self._path.unlink()
            Path(tmp_path).rename(self._path)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
