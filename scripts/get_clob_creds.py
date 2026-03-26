"""
Run once to derive Polymarket CLOB API credentials from your wallet's private key.
Store the output in your .env file.

Usage:
    python scripts/get_clob_creds.py
"""
import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from eth_account import Account

load_dotenv()

PRIVATE_KEY = os.getenv("REAL_WALLET_PRIVATE_KEY")
if not PRIVATE_KEY:
    print("ERROR: Set REAL_WALLET_PRIVATE_KEY in your .env first")
    exit(1)

# Show the wallet address derived from the private key
account = Account.from_key(PRIVATE_KEY)
print(f"Wallet address: {account.address}")
print("Make sure this address is connected to Polymarket before continuing.\n")

client = ClobClient(
    host="https://clob.polymarket.com",
    key=PRIVATE_KEY,
    chain_id=137,
)

# Try derive first, then create if it fails
for method_name, method in [("derive_api_key", client.derive_api_key), ("create_api_key", client.create_api_key)]:
    try:
        print(f"Trying {method_name}()...")
        creds = method()
        print(f"\n✅ Success with {method_name}! Add these to your .env:\n")
        print(f"CLOB_API_KEY={creds.api_key}")
        print(f"CLOB_SECRET={creds.api_secret}")
        print(f"CLOB_PASSPHRASE={creds.api_passphrase}")
        exit(0)
    except Exception as e:
        print(f"  → Failed: {e}")

print("\n❌ Both methods failed. The wallet may not be registered on Polymarket yet.")
