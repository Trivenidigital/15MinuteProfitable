"""Pre-flight validation for Polymarket live trading credentials.

Tests the entire signing pipeline WITHOUT running the full bot.
Validates: private key, funder, signature_type, API creds, neg_risk,
network connectivity, wallet balance, and order signing.

Usage:
    python scripts/preflight_test.py          # full test (posts + cancels a $0.01 order)
    python scripts/preflight_test.py --dry    # sign-only test (no order posted)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to path so we can import src
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _pass(msg: str) -> None:
    print(f"  [PASS] {msg}")


def _fail(msg: str, detail: str = "") -> None:
    print(f"  [FAIL] {msg}")
    if detail:
        print(f"         {detail}")


def _info(msg: str) -> None:
    print(f"  [INFO] {msg}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-flight credential test")
    parser.add_argument(
        "--dry",
        action="store_true",
        help="Sign-only test — don't post the order to the CLOB",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  Polymarket Pre-Flight Test")
    print("=" * 60)
    print()

    passed = 0
    failed = 0

    # ------------------------------------------------------------------
    # Step 1: Load settings
    # ------------------------------------------------------------------
    print("Step 1: Loading settings from .env ...")
    try:
        from src.config import Settings

        settings = Settings()
        _pass("Settings loaded")
        _info(f"  clob_host    = {settings.clob_host}")
        _info(f"  sig_type     = {int(settings.signature_type)}")
        _info(f"  funder       = {settings.funder or '(empty)'}")
        _info(f"  neg_risk     = {settings.neg_risk}")
        _info(f"  dry_run      = {settings.dry_run}")
        passed += 1
    except Exception as exc:
        _fail("Could not load settings", str(exc))
        failed += 1
        print("\nAborting — fix .env first.")
        sys.exit(1)

    # Warn if funder is empty with sig_type=1
    if int(settings.signature_type) == 1 and not settings.funder:
        _fail(
            "funder is empty with signature_type=1",
            "Set BOT_FUNDER to your Polymarket proxy wallet address",
        )
        failed += 1

    # ------------------------------------------------------------------
    # Step 2: Create ClobClient and derive API creds
    # ------------------------------------------------------------------
    print()
    print("Step 2: Creating ClobClient and deriving API credentials ...")
    try:
        from py_clob_client.client import ClobClient

        client = ClobClient(
            host=settings.clob_host,
            key=settings.private_key.get_secret_value(),
            chain_id=137,
            signature_type=int(settings.signature_type),
            funder=settings.funder or None,
        )
        _pass("ClobClient created")
        passed += 1
    except Exception as exc:
        _fail("ClobClient creation failed", str(exc))
        failed += 1
        print("\nAborting — check private key and signature_type.")
        sys.exit(1)

    try:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        _pass("API credentials derived and set")
        passed += 1
    except Exception as exc:
        _fail("API credential derivation failed", str(exc))
        _info("Common causes: wrong key, wrong funder, wrong sig_type, network issue")
        failed += 1

    # ------------------------------------------------------------------
    # Step 3: Fetch active markets (network connectivity test)
    # ------------------------------------------------------------------
    print()
    print("Step 3: Fetching active markets from Gamma API ...")
    try:
        import urllib.request
        import json

        url = f"{settings.gamma_api_url}/markets?limit=5&active=true&closed=false"
        req = urllib.request.Request(url, headers={"User-Agent": "preflight-test/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            markets = json.loads(resp.read())

        if markets:
            _pass(f"Gamma API reachable — {len(markets)} active markets found")
            passed += 1
        else:
            _fail("Gamma API returned zero markets")
            failed += 1
    except Exception as exc:
        _fail("Gamma API request failed", str(exc))
        failed += 1

    # ------------------------------------------------------------------
    # Step 4: Fetch USDC balance
    # ------------------------------------------------------------------
    print()
    print("Step 4: Checking wallet balance ...")
    try:
        balance_resp = client.get_balance_allowance()
        if isinstance(balance_resp, dict):
            balance_wei = int(balance_resp.get("balance", 0))
        else:
            balance_wei = int(getattr(balance_resp, "balance", 0))

        balance_usd = balance_wei / 1_000_000  # USDC has 6 decimals
        if balance_usd > 0:
            _pass(f"Wallet balance: ${balance_usd:.2f} USDC")
            passed += 1
        else:
            _fail("Wallet balance is $0.00", "Deposit USDC to trade")
            failed += 1
    except Exception as exc:
        _fail("Balance check failed", str(exc))
        _info("This may indicate invalid API credentials")
        failed += 1

    # ------------------------------------------------------------------
    # Step 5: Sign a test order
    # ------------------------------------------------------------------
    print()
    print("Step 5: Signing a $0.01 test order ...")
    try:
        from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY

        # Find a valid token_id from active markets
        token_id = None
        try:
            url = f"{settings.gamma_api_url}/markets?limit=1&active=true&closed=false"
            req = urllib.request.Request(url, headers={"User-Agent": "preflight-test/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                mkt_data = json.loads(resp.read())
            if mkt_data:
                # Try to get a token from the market's CLOB token IDs
                mkt = mkt_data[0]
                tokens = mkt.get("clobTokenIds") or mkt.get("tokens", [])
                if isinstance(tokens, str):
                    token_id = tokens.split(",")[0].strip()
                elif isinstance(tokens, list) and tokens:
                    if isinstance(tokens[0], dict):
                        token_id = tokens[0].get("token_id", "")
                    else:
                        token_id = str(tokens[0])
        except Exception:
            pass

        if not token_id:
            _fail("Could not find a valid token_id for test order")
            failed += 1
        else:
            order_args = OrderArgs(
                token_id=token_id,
                price=0.01,
                size=1.0,
                side=BUY,
            )
            options = PartialCreateOrderOptions(
                tick_size="0.01",
                neg_risk=settings.neg_risk,
            )
            signed = client.create_order(order_args, options)
            _pass("Order signed successfully")
            _info(f"  token_id = {token_id[:20]}...")
            passed += 1

            # ------------------------------------------------------------------
            # Step 6: Post and cancel (unless --dry)
            # ------------------------------------------------------------------
            if args.dry:
                print()
                _info("--dry mode: skipping order submission")
            else:
                print()
                print("Step 6: Posting order and immediately cancelling ...")
                try:
                    post_resp = client.post_order(signed)
                    if isinstance(post_resp, dict):
                        order_id = post_resp.get("orderID") or post_resp.get("id", "")
                    else:
                        order_id = getattr(post_resp, "orderID", "") or getattr(post_resp, "id", "")

                    _pass(f"Order posted (id={order_id})")
                    passed += 1

                    if order_id:
                        try:
                            client.cancel(order_id)
                            _pass("Order cancelled")
                            passed += 1
                        except Exception as exc:
                            _fail("Cancel failed (order may have expired already)", str(exc))
                            failed += 1
                except Exception as exc:
                    _fail("Order submission failed", str(exc))
                    _info("This is the most common failure point — check:")
                    _info("  - funder address matches proxy wallet")
                    _info("  - signature_type matches account type")
                    _info("  - neg_risk matches market type")
                    failed += 1

    except Exception as exc:
        _fail("Order signing failed", str(exc))
        _info("Check: private key, funder, signature_type, neg_risk")
        failed += 1

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print()
    print("=" * 60)
    total = passed + failed
    if failed == 0:
        print(f"  ALL {total} CHECKS PASSED")
        print("  Your credentials and signing pipeline are working correctly.")
    else:
        print(f"  {passed}/{total} checks passed, {failed} FAILED")
        print("  Fix the failed checks before going live.")
    print("=" * 60)

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
