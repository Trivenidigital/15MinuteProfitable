"""Generate Polymarket L2 API credentials from your private key.

Usage:
    .venv\Scripts\python.exe scripts\setup_api_key.py

Prerequisites:
    1. A Polygon wallet private key (from MetaMask, etc.)
    2. A funded Polymarket account (deposit USDC at polymarket.com)

This script will:
    1. Ask for your private key
    2. Ask for your signature type (EOA or Magic.link/Polymarket wallet)
    3. Derive or create L2 API credentials
    4. Print the .env values you need
"""

import sys


def main() -> None:
    print("=" * 60)
    print("  Polymarket L2 API Key Setup")
    print("=" * 60)
    print()

    # Step 1: Private key
    print("Step 1: Enter your Polygon wallet private key")
    print()
    print("  HOW TO FIND IT in MetaMask:")
    print("    1. Open MetaMask extension")
    print("    2. Click the 3 dots (⋮) next to your account name")
    print("    3. Click 'Account details'")
    print("    4. Click 'Show private key'")
    print("    5. Enter your MetaMask password")
    print("    6. Copy the key shown (starts with 0x, 66 chars)")
    print()
    print("  NOTE: If MetaMask shows per-chain keys, click the copy")
    print("  icon next to ANY chain — they're all the same key.")
    print()
    private_key = input("  Private Key: ").strip()

    # Auto-fix: add 0x prefix if missing
    if not private_key.startswith("0x") and len(private_key) == 64:
        private_key = "0x" + private_key
        print("  (added 0x prefix automatically)")

    # Validate
    if len(private_key) == 42:
        print("\n  ERROR: That's a wallet ADDRESS (42 chars), not a private key.")
        print("  A private key is 66 characters (0x + 64 hex chars).")
        print("  Go to MetaMask > Account Details > Show Private Key.")
        sys.exit(1)

    if not private_key.startswith("0x") or len(private_key) != 66:
        print(f"\n  ERROR: Key must start with 0x and be 66 characters.")
        print(f"  You entered {len(private_key)} characters.")
        print(f"  Example: 0x" + "a" * 64)
        sys.exit(1)

    # Check it's valid hex
    try:
        int(private_key, 16)
    except ValueError:
        print("\n  ERROR: Key contains non-hex characters.")
        print("  It should only contain 0-9 and a-f after the 0x prefix.")
        sys.exit(1)

    # Step 2: Signature type
    print()
    print("Step 2: How did you create your Polymarket account?")
    print("  [1] MetaMask / hardware wallet / EOA (you connected directly)")
    print("  [2] Email / Magic.link / Polymarket app (most common)")
    print()
    choice = input("  Choice (1 or 2): ").strip()

    if choice == "1":
        sig_type = 0  # EOA
        funder = ""
        print("\n  Using signature_type=0 (EOA)")
    elif choice == "2":
        sig_type = 1  # POLY_GNOSIS_SAFE
        print("\n  Using signature_type=1 (Polymarket proxy wallet)")
        print()
        print("  You need your PROXY WALLET address.")
        print("  Find it at: polymarket.com > Profile > your deposit address")
        print("  (This is NOT your MetaMask address — it's the Polymarket smart wallet)")
        print()
        funder = input("  Proxy Wallet Address (0x...): ").strip()
        if not funder.startswith("0x") or len(funder) != 42:
            print("\n  ERROR: Address must start with 0x and be 42 characters.")
            sys.exit(1)
    else:
        print("\n  ERROR: Choose 1 or 2.")
        sys.exit(1)

    # Step 3: Derive API creds
    print()
    print("Step 3: Deriving API credentials...")
    print("  (this signs a message with your key — no gas cost)")
    print()

    try:
        from py_clob_client.client import ClobClient

        client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=137,
            signature_type=sig_type,
            funder=funder if funder else None,
        )

        # derive_api_key creates or retrieves existing API credentials
        creds = client.derive_api_key()
        print("  SUCCESS! API credentials derived.")
        print()

    except Exception as exc:
        print(f"  ERROR: {exc}")
        print()
        print("  Common issues:")
        print("  - Wrong private key")
        print("  - Wrong signature type (try the other option)")
        print("  - Wrong funder/proxy address")
        print("  - No internet connection")
        print("  - Account not yet created on polymarket.com")
        sys.exit(1)

    # Step 4: Verify by fetching balance
    print("Step 4: Verifying connection...")
    try:
        client.set_api_creds(client.create_or_derive_api_creds())
        print("  API credentials verified!")
    except Exception:
        print("  Warning: Could not verify creds, but they may still work.")

    # Step 5: Print .env
    print()
    print("=" * 60)
    print("  Add these to your .env file:")
    print("=" * 60)
    print()
    print(f"BOT_PRIVATE_KEY={private_key}")
    print(f"BOT_SIGNATURE_TYPE={sig_type}")
    if funder:
        print(f"BOT_FUNDER={funder}")
    print("BOT_DRY_RUN=true")
    print("BOT_DASHBOARD_ENABLED=true")
    print()
    print("  API key is derived automatically at runtime from your")
    print("  private key — no need to store it separately.")
    print()
    print("  Start the bot with:")
    print('  .venv\\Scripts\\python.exe -m src.main')
    print()
    print("  Dashboard at: http://127.0.0.1:8080")
    print()
    print("  IMPORTANT: Keep BOT_DRY_RUN=true until you've verified")
    print("  everything works correctly!")
    print("=" * 60)


if __name__ == "__main__":
    main()
