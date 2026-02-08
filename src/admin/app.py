"""FastAPI admin dashboard for managing bot settings, secrets, and control.

Runs as a separate process on HTTPS port 8443 (configurable).
The monitoring dashboard at :8080 remains untouched.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from src.admin.env_manager import read_env, write_env
from src.config import Settings
from src.utils.vault import SecretVault, VaultError

# ---------------------------------------------------------------------------
# Template setup
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_security = HTTPBasic()


def _get_admin_credentials() -> tuple[str, str]:
    """Read admin username/password from env vars."""
    username = os.environ.get("ADMIN_USERNAME", "")
    password = os.environ.get("ADMIN_PASSWORD", "")
    return username, password


def verify_admin(
    credentials: HTTPBasicCredentials = Depends(_security),
) -> str:
    """Verify HTTP Basic credentials.  Returns username on success."""
    expected_user, expected_pass = _get_admin_credentials()
    if not expected_user or not expected_pass:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_USERNAME and ADMIN_PASSWORD must be set",
        )
    user_ok = secrets.compare_digest(credentials.username.encode(), expected_user.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), expected_pass.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SERVICE_NAME = "btc15minutebot"


def _env_path() -> Path:
    return Path(os.environ.get("BOT_ENV_FILE", ".env"))


def _vault_path() -> Path:
    return Path(os.environ.get("VAULT_PATH", "data/secrets.vault"))


# Secret fields that should NOT appear in .env settings API
_SECRET_ENV_KEYS = {
    "BOT_PRIVATE_KEY",
    "BOT_TELEGRAM_BOT_TOKEN",
    "BOT_DISCORD_WEBHOOK_URL",
    "BOT_DASHBOARD_PASSWORD",
    "BOT_FUNDER",
}


def _get_vault() -> SecretVault | None:
    """Return a SecretVault instance if VAULT_PASSWORD is configured."""
    vault_password = os.environ.get("VAULT_PASSWORD", "")
    if not vault_password:
        return None
    return SecretVault(_vault_path(), vault_password)


_WRITE_ONLY_SECRETS = {"private_key"}


def _load_secrets_for_display() -> dict[str, str]:
    """Load secrets from vault (or .env fallback) for the wallet page.

    Write-only secrets (private_key) are replaced with a sentinel value
    so the template can show "Stored" status without exposing the actual
    value to the browser.
    """
    vault = _get_vault()
    raw: dict[str, str] = {}
    if vault and vault.exists():
        try:
            raw = vault.load()
        except VaultError:
            pass

    if not raw:
        # Fallback: read from .env
        env_data = read_env(_env_path())
        mapping = {
            "BOT_PRIVATE_KEY": "private_key",
            "BOT_TELEGRAM_BOT_TOKEN": "telegram_bot_token",
            "BOT_DISCORD_WEBHOOK_URL": "discord_webhook_url",
            "BOT_DASHBOARD_PASSWORD": "dashboard_password",
            "BOT_FUNDER": "funder",
        }
        for env_key, vault_key in mapping.items():
            val = env_data.get(env_key, "")
            if val:
                raw[vault_key] = val

    # Redact write-only secrets: keep a truthy sentinel so the template
    # can detect "is stored" but never expose the actual value.
    result: dict[str, str] = {}
    for key, val in raw.items():
        if key in _WRITE_ONLY_SECRETS and val:
            result[key] = "********"
        else:
            result[key] = val
    return result


def _load_settings() -> Settings:
    """Load Settings from current .env + vault (same as bot would)."""
    return Settings()  # type: ignore[call-arg]


def _make_clob_client(private_key: str, signature_type: int, funder: str | None) -> object:
    """Create a ClobClient instance (isolated for testability)."""
    from py_clob_client.client import ClobClient

    return ClobClient(
        host="https://clob.polymarket.com",
        key=private_key,
        chain_id=137,
        signature_type=signature_type,
        funder=funder,
    )


def _run_systemctl(action: str) -> tuple[bool, str]:
    """Run ``systemctl <action> <service>`` and return (success, output)."""
    try:
        result = subprocess.run(
            ["systemctl", action, _SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, "systemctl not found (not running on Linux?)"
    except subprocess.TimeoutExpired:
        return False, "systemctl timed out"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_admin_app() -> FastAPI:
    """Create the admin dashboard FastAPI application."""
    app = FastAPI(
        title="BTC15MinuteBot Admin",
        version="1.0.0",
    )

    # ------------------------------------------------------------------
    # HTML pages
    # ------------------------------------------------------------------

    @app.get("/")
    async def settings_page(
        request: Request,
        _user: str = Depends(verify_admin),
    ):  # type: ignore[no-untyped-def]
        settings = _load_settings()
        return templates.TemplateResponse(
            request,
            "settings.html",
            {"settings": settings, "active_tab": "settings"},
        )

    @app.get("/wallet")
    async def wallet_page(
        request: Request,
        _user: str = Depends(verify_admin),
    ):  # type: ignore[no-untyped-def]
        settings = _load_settings()
        vault_secrets = _load_secrets_for_display()
        return templates.TemplateResponse(
            request,
            "wallet.html",
            {
                "settings": settings,
                "secrets": vault_secrets,
                "active_tab": "wallet",
            },
        )

    @app.get("/control")
    async def control_page(
        request: Request,
        _user: str = Depends(verify_admin),
    ):  # type: ignore[no-untyped-def]
        return templates.TemplateResponse(
            request,
            "control.html",
            {"active_tab": "control"},
        )

    # ------------------------------------------------------------------
    # Settings API
    # ------------------------------------------------------------------

    @app.post("/api/settings")
    async def save_settings(
        request: Request,
        _user: str = Depends(verify_admin),
    ) -> JSONResponse:
        """Save non-secret settings to .env file."""
        body = await request.json()

        # Filter out secret keys — those go through /api/secrets.
        # Skip empty string values so Pydantic code defaults apply
        # instead of writing unparesable "" for numeric fields.
        updates: dict[str, str] = {}
        for key, value in body.items():
            if key in _SECRET_ENV_KEYS:
                continue
            str_val = str(value)
            if str_val == "":
                continue
            updates[key] = str_val

        write_env(_env_path(), updates)

        # Sync updated values into os.environ so that Settings() picks
        # them up immediately (env vars take priority over dotenv).
        for key, value in updates.items():
            os.environ[key] = value

        return JSONResponse({"status": "ok", "updated": len(updates)})

    # ------------------------------------------------------------------
    # Secrets API
    # ------------------------------------------------------------------

    @app.post("/api/secrets")
    async def save_secrets(
        request: Request,
        _user: str = Depends(verify_admin),
    ) -> JSONResponse:
        """Save secrets to the encrypted vault."""
        body: dict[str, str] = await request.json()

        vault = _get_vault()
        if vault is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="VAULT_PASSWORD not set — cannot write to vault",
            )

        # Load existing secrets (or start fresh)
        if vault.exists():
            try:
                existing = vault.load()
            except VaultError as exc:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to read vault: {exc}",
                ) from exc
        else:
            existing = {}

        # Merge new secrets (empty values = keep existing for write-only,
        # delete for others)
        for key, value in body.items():
            if key in _WRITE_ONLY_SECRETS:
                # Only update if user provided a new value
                if value and value != "********":
                    existing[key] = value
                # Otherwise keep the existing stored value
            elif value:
                existing[key] = value
            else:
                existing.pop(key, None)

        try:
            vault.save(existing)
        except VaultError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to write vault: {exc}",
            ) from exc

        return JSONResponse({"status": "ok", "keys": len(existing)})

    # ------------------------------------------------------------------
    # Wallet derive API
    # ------------------------------------------------------------------

    @app.post("/api/wallet/derive")
    async def wallet_derive(
        request: Request,
        _user: str = Depends(verify_admin),
    ) -> JSONResponse:
        """Derive ETH address and signature type from private key."""
        body = await request.json()
        private_key: str = body.get("private_key", "").strip()
        polymarket_address: str = body.get("polymarket_address", "").strip()

        if not private_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="private_key is required",
            )

        # Derive ETH address
        try:
            from eth_account import Account

            acct = Account.from_key(private_key)
            eth_address = acct.address
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid private key: {exc}",
            ) from exc

        # Auto-determine signature type
        if polymarket_address:
            signature_type = 1  # POLY_GNOSIS_SAFE
            funder = polymarket_address
        else:
            signature_type = 0  # EOA
            funder = ""

        # Best-effort API verification
        api_verified = False
        api_key_preview = ""
        api_error = ""
        try:
            clob = _make_clob_client(private_key, signature_type, funder or None)
            api_creds = clob.derive_api_key()  # type: ignore[attr-defined]
            api_verified = True
            api_key_preview = str(api_creds.get("apiKey", ""))[:12] + "..."
        except Exception as exc:
            api_error = str(exc)

        return JSONResponse(
            {
                "eth_address": eth_address,
                "signature_type": signature_type,
                "funder": funder,
                "api_verified": api_verified,
                "api_key_preview": api_key_preview,
                "api_error": api_error,
            }
        )

    # ------------------------------------------------------------------
    # Bot control API
    # ------------------------------------------------------------------

    @app.post("/api/bot/start")
    async def bot_start(
        _user: str = Depends(verify_admin),
    ) -> JSONResponse:
        ok, output = _run_systemctl("start")
        if not ok:
            raise HTTPException(status_code=500, detail=output)
        return JSONResponse({"status": "ok", "output": output})

    @app.post("/api/bot/stop")
    async def bot_stop(
        _user: str = Depends(verify_admin),
    ) -> JSONResponse:
        ok, output = _run_systemctl("stop")
        if not ok:
            raise HTTPException(status_code=500, detail=output)
        return JSONResponse({"status": "ok", "output": output})

    @app.post("/api/bot/restart")
    async def bot_restart(
        _user: str = Depends(verify_admin),
    ) -> JSONResponse:
        ok, output = _run_systemctl("restart")
        if not ok:
            raise HTTPException(status_code=500, detail=output)
        return JSONResponse({"status": "ok", "output": output})

    @app.get("/api/bot/status")
    async def bot_status(
        _user: str = Depends(verify_admin),
    ) -> JSONResponse:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", _SERVICE_NAME],
                capture_output=True,
                text=True,
                timeout=10,
            )
            active_status = result.stdout.strip()

            # Get uptime if active
            uptime = ""
            if active_status == "active":
                show_result = subprocess.run(
                    ["systemctl", "show", _SERVICE_NAME, "--property=ActiveEnterTimestamp"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                uptime = show_result.stdout.strip().replace("ActiveEnterTimestamp=", "")

            return JSONResponse(
                {
                    "status": active_status,
                    "uptime": uptime,
                }
            )
        except FileNotFoundError:
            return JSONResponse(
                {
                    "status": "unknown",
                    "uptime": "",
                    "error": "systemctl not found",
                }
            )

    # ------------------------------------------------------------------
    # Vault management API
    # ------------------------------------------------------------------

    @app.get("/api/vault/status")
    async def vault_status(
        _user: str = Depends(verify_admin),
    ) -> JSONResponse:
        vault = _get_vault()
        if vault is None:
            return JSONResponse(
                {
                    "exists": False,
                    "unlocked": False,
                    "key_count": 0,
                    "error": "VAULT_PASSWORD not configured",
                }
            )

        exists = vault.exists()
        key_count = 0
        unlocked = False

        if exists:
            try:
                data = vault.load()
                key_count = len(data)
                unlocked = True
            except VaultError:
                unlocked = False

        return JSONResponse(
            {
                "exists": exists,
                "unlocked": unlocked,
                "key_count": key_count,
            }
        )

    @app.post("/api/vault/init")
    async def vault_init(
        _user: str = Depends(verify_admin),
    ) -> JSONResponse:
        """Initialize a new empty vault."""
        vault = _get_vault()
        if vault is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="VAULT_PASSWORD not set",
            )
        if vault.exists():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Vault already exists",
            )

        try:
            vault.create({})
        except VaultError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create vault: {exc}",
            ) from exc

        return JSONResponse({"status": "ok"})

    @app.post("/api/vault/change-password")
    async def vault_change_password(
        request: Request,
        _user: str = Depends(verify_admin),
    ) -> JSONResponse:
        """Re-encrypt vault with a new master password."""
        body = await request.json()
        new_password = body.get("new_password", "")
        if not new_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="new_password is required",
            )

        vault = _get_vault()
        if vault is None or not vault.exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Vault not available",
            )

        try:
            vault.change_password(new_password)
        except VaultError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to re-encrypt vault: {exc}",
            ) from exc

        return JSONResponse(
            {
                "status": "ok",
                "message": "Vault re-encrypted. Update VAULT_PASSWORD env var.",
            }
        )

    return app
