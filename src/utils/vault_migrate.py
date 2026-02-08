"""Migrate secrets from ``.env`` to an encrypted vault.

Usage::

    # Interactive (prompts for password)
    python -m src.utils.vault_migrate

    # Non-interactive
    VAULT_PASSWORD=changeme python -m src.utils.vault_migrate
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from src.admin.env_manager import read_env, remove_keys
from src.utils.vault import SecretVault, VaultError

# .env keys that contain secrets (BOT_ prefix used by pydantic-settings)
_SECRET_ENV_KEYS = {
    "BOT_PRIVATE_KEY",
    "BOT_TELEGRAM_BOT_TOKEN",
    "BOT_DISCORD_WEBHOOK_URL",
    "BOT_DASHBOARD_PASSWORD",
    "BOT_FUNDER",
}

# Mapping from .env key -> vault key (strip BOT_ prefix, lowercase)
_ENV_TO_VAULT: dict[str, str] = {
    "BOT_PRIVATE_KEY": "private_key",
    "BOT_TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "BOT_DISCORD_WEBHOOK_URL": "discord_webhook_url",
    "BOT_DASHBOARD_PASSWORD": "dashboard_password",
    "BOT_FUNDER": "funder",
}

_DEFAULT_ENV = Path(".env")
_DEFAULT_VAULT = Path("data/secrets.vault")


def migrate(
    env_path: Path = _DEFAULT_ENV,
    vault_path: Path = _DEFAULT_VAULT,
    password: str | None = None,
    *,
    remove_from_env: bool = True,
) -> dict[str, str]:
    """Run the migration.

    Returns
    -------
    dict[str, str]
        The secrets that were migrated (vault keys -> values).
    """
    # 1. Read .env
    env_data = read_env(env_path)

    # 2. Extract secrets
    secrets: dict[str, str] = {}
    for env_key, vault_key in _ENV_TO_VAULT.items():
        value = env_data.get(env_key, "")
        if value:
            secrets[vault_key] = value

    if not secrets:
        print("No secrets found in .env — nothing to migrate.")
        return {}

    # 3. Get vault password
    if password is None:
        password = os.environ.get("VAULT_PASSWORD", "")
    if not password:
        password = getpass.getpass("Enter vault master password: ")
    if not password:
        print("Error: vault password is required.", file=sys.stderr)
        sys.exit(1)

    # 4. Create or update vault
    vault = SecretVault(vault_path, password)
    if vault.exists():
        existing = vault.load()
        existing.update(secrets)
        vault.save(existing)
        print(f"Updated existing vault at {vault_path}")
    else:
        vault.create(secrets)
        print(f"Created new vault at {vault_path}")

    # 5. Remove secrets from .env
    if remove_from_env:
        found_keys = {k for k in _SECRET_ENV_KEYS if env_data.get(k)}
        if found_keys:
            remove_keys(env_path, found_keys)
            print(f"Removed {len(found_keys)} secret(s) from {env_path}")

    # 6. Summary
    print("\nMigrated secrets:")
    for vault_key in sorted(secrets.keys()):
        val = secrets[vault_key]
        masked = val[:4] + "..." + val[-4:] if len(val) > 12 else "****"
        print(f"  {vault_key}: {masked}")

    print(f"\nTotal: {len(secrets)} secret(s) migrated to vault.")
    return secrets


if __name__ == "__main__":
    try:
        migrate()
    except VaultError as exc:
        print(f"Vault error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
