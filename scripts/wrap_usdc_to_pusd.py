"""
Wrap USDC.e → pUSD via Polymarket CollateralOnramp (V2 migration).

Executes TWO on-chain transactions:
  1. approve(onramp, amount)  on USDC.e   — authorize the onramp to pull USDC.e
  2. wrap(USDC.e, dest, amount) on onramp — exchange USDC.e for pUSD 1:1

The proxy wallet (Gnosis Safe) is the source of funds for most Polymarket API
traders, so both txs go through the Safe via execTransaction(). If no proxy
wallet is configured, the EOA is the source and executes directly.

Usage:
    PYTHONPATH=. python3 scripts/wrap_usdc_to_pusd.py --amount 100 --dry-run
    PYTHONPATH=. python3 scripts/wrap_usdc_to_pusd.py --amount 100 --execute

Default is dry-run — prints what would happen without signing/broadcasting.
"""

import argparse
import sys

from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from config.settings import (
    COLLATERAL_ONRAMP,
    HTTP_RPC_URL,
    PUSD_ADDRESS,
    REAL_PROXY_WALLET,
    REAL_WALLET_PRIVATE_KEY,
    USDC_ADDRESS,
)

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

ONRAMP_ABI = [
    {"name": "wrap", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "asset", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "outputs": []},
]

SAFE_ABI = [
    {"name": "nonce", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "getTransactionHash", "type": "function", "stateMutability": "view",
     "inputs": [
         {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
         {"name": "data", "type": "bytes"}, {"name": "operation", "type": "uint8"},
         {"name": "safeTxGas", "type": "uint256"}, {"name": "baseGas", "type": "uint256"},
         {"name": "gasPrice", "type": "uint256"}, {"name": "gasToken", "type": "address"},
         {"name": "refundReceiver", "type": "address"}, {"name": "_nonce", "type": "uint256"},
     ],
     "outputs": [{"name": "", "type": "bytes32"}]},
    {"name": "execTransaction", "type": "function", "stateMutability": "payable",
     "inputs": [
         {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
         {"name": "data", "type": "bytes"}, {"name": "operation", "type": "uint8"},
         {"name": "safeTxGas", "type": "uint256"}, {"name": "baseGas", "type": "uint256"},
         {"name": "gasPrice", "type": "uint256"}, {"name": "gasToken", "type": "address"},
         {"name": "refundReceiver", "type": "address"}, {"name": "signatures", "type": "bytes"},
     ],
     "outputs": [{"name": "", "type": "bool"}]},
]

ZERO_ADDR = "0x0000000000000000000000000000000000000000"


def _w3() -> Web3:
    w3 = Web3(Web3.HTTPProvider(HTTP_RPC_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def _safe_exec(
    w3: Web3,
    account,
    safe_addr: str,
    target: str,
    calldata: bytes,
    gas_limit: int,
) -> str:
    """Execute a call through a Gnosis Safe. Returns tx hash hex."""
    safe       = w3.eth.contract(address=Web3.to_checksum_address(safe_addr), abi=SAFE_ABI)
    safe_nonce = safe.functions.nonce().call()

    safe_tx_hash = safe.functions.getTransactionHash(
        Web3.to_checksum_address(target), 0, calldata,
        0, 0, 0, 0, ZERO_ADDR, ZERO_ADDR,
        safe_nonce,
    ).call()
    signed_safe = Account._sign_hash(safe_tx_hash, private_key=REAL_WALLET_PRIVATE_KEY)

    nonce = w3.eth.get_transaction_count(account.address)
    tx = safe.functions.execTransaction(
        Web3.to_checksum_address(target), 0, calldata,
        0, 0, 0, 0, ZERO_ADDR, ZERO_ADDR,
        signed_safe.signature,
    ).build_transaction({
        "from": account.address, "nonce": nonce,
        "gas": gas_limit, "gasPrice": int(w3.eth.gas_price * 1.3),
        "chainId": 137,
    })
    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"tx reverted: {tx_hash.hex()}")
    return tx_hash.hex()


def _direct_tx(w3: Web3, account, target: str, calldata: bytes, gas_limit: int) -> str:
    nonce = w3.eth.get_transaction_count(account.address)
    tx = {
        "from": account.address, "to": Web3.to_checksum_address(target),
        "nonce": nonce, "data": calldata, "value": 0,
        "gas": gas_limit, "gasPrice": int(w3.eth.gas_price * 1.3),
        "chainId": 137,
    }
    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] != 1:
        raise RuntimeError(f"tx reverted: {tx_hash.hex()}")
    return tx_hash.hex()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--amount", type=float, required=True, help="USDC.e amount to wrap")
    parser.add_argument("--execute", action="store_true", help="Actually broadcast txs (default = dry-run)")
    args = parser.parse_args()

    w3      = _w3()
    account = Account.from_key(REAL_WALLET_PRIVATE_KEY)
    source  = REAL_PROXY_WALLET or account.address
    amount  = int(args.amount * 1_000_000)  # USDC.e has 6 decimals

    usdc   = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS),  abi=ERC20_ABI)
    pusd   = w3.eth.contract(address=Web3.to_checksum_address(PUSD_ADDRESS),  abi=ERC20_ABI)
    onramp = w3.eth.contract(address=Web3.to_checksum_address(COLLATERAL_ONRAMP), abi=ONRAMP_ABI)

    usdc_bal_before = usdc.functions.balanceOf(Web3.to_checksum_address(source)).call() / 1_000_000
    pusd_bal_before = pusd.functions.balanceOf(Web3.to_checksum_address(source)).call() / 1_000_000
    allowance       = usdc.functions.allowance(
        Web3.to_checksum_address(source), Web3.to_checksum_address(COLLATERAL_ONRAMP)
    ).call() / 1_000_000

    print(f"Source wallet : {source}  ({'proxy/safe' if REAL_PROXY_WALLET else 'EOA'})")
    print(f"USDC.e balance: {usdc_bal_before:.2f}")
    print(f"pUSD   balance: {pusd_bal_before:.2f}")
    print(f"Onramp allowance (USDC.e): {allowance:.2f}")
    print(f"Planned wrap amount     : {args.amount:.2f} USDC.e → {args.amount:.2f} pUSD (1:1)")

    if usdc_bal_before < args.amount:
        print(f"❌ Insufficient USDC.e balance ({usdc_bal_before:.2f} < {args.amount:.2f})")
        return 1

    if not args.execute:
        print("\n[DRY-RUN] Use --execute to broadcast txs.")
        return 0

    # --- Step 1: approve if needed ---
    if allowance < args.amount:
        approve_data = usdc.encode_abi("approve", args=[Web3.to_checksum_address(COLLATERAL_ONRAMP), amount])
        approve_bytes = bytes.fromhex(approve_data[2:])
        print(f"\n[1/2] Approving {args.amount:.2f} USDC.e to onramp…")
        if REAL_PROXY_WALLET:
            tx1 = _safe_exec(w3, account, REAL_PROXY_WALLET, USDC_ADDRESS, approve_bytes, 250_000)
        else:
            tx1 = _direct_tx(w3, account, USDC_ADDRESS, approve_bytes, 100_000)
        print(f"       ✅ approve tx: {tx1}")
    else:
        print("\n[1/2] Allowance already sufficient — skipping approve.")

    # --- Step 2: wrap ---
    wrap_data  = onramp.encode_abi("wrap", args=[
        Web3.to_checksum_address(USDC_ADDRESS),
        Web3.to_checksum_address(source),
        amount,
    ])
    wrap_bytes = bytes.fromhex(wrap_data[2:])
    print(f"\n[2/2] Wrapping {args.amount:.2f} USDC.e → pUSD…")
    if REAL_PROXY_WALLET:
        tx2 = _safe_exec(w3, account, REAL_PROXY_WALLET, COLLATERAL_ONRAMP, wrap_bytes, 400_000)
    else:
        tx2 = _direct_tx(w3, account, COLLATERAL_ONRAMP, wrap_bytes, 200_000)
    print(f"       ✅ wrap tx   : {tx2}")

    # --- Verify ---
    usdc_bal_after = usdc.functions.balanceOf(Web3.to_checksum_address(source)).call() / 1_000_000
    pusd_bal_after = pusd.functions.balanceOf(Web3.to_checksum_address(source)).call() / 1_000_000
    print(f"\nUSDC.e: {usdc_bal_before:.2f} → {usdc_bal_after:.2f}  (Δ {usdc_bal_after - usdc_bal_before:+.2f})")
    print(f"pUSD  : {pusd_bal_before:.2f} → {pusd_bal_after:.2f}  (Δ {pusd_bal_after - pusd_bal_before:+.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
