"""One-time Polymarket on-chain approval script.

Run this ONCE per wallet, after funding it with USDC.e + a tiny MATIC balance
for gas. After approvals, the bot can place orders without paying gas per
trade (CLOB orders are off-chain signed; only redemption at settlement is
on-chain).

Usage:
    PK=0xYOUR_PRIVATE_KEY \\
    WALLET=0xYOUR_WALLET_ADDRESS \\
    POLYGON_RPC=https://polygon-rpc.com \\
    uv run python scripts/polymarket_approve.py

Sets:
  - USDC.e -> max approve to: CTF Exchange, Neg Risk Exchange, Router
  - Conditional Tokens -> setApprovalForAll(true) to those same 3 spenders

Idempotent: if an approval already exists at max, the script skips it.
"""

from __future__ import annotations

import os
import sys
import time

from web3 import Web3

USDCE = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
ROUTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CHAIN_ID = 137

MAX_UINT256 = 2**256 - 1
HALF_MAX = 1 << 255

ERC20_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]

CTF_ABI = [
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


def main() -> int:
    pk = os.getenv("PK") or os.getenv("POLYMARKET_PRIVATE_KEY")
    wallet = os.getenv("WALLET") or os.getenv("POLYMARKET_WALLET")
    rpc = os.getenv("POLYGON_RPC") or "https://polygon-rpc.com"
    if not pk or not wallet:
        print("ERROR: set PK and WALLET env vars before running.", file=sys.stderr)
        return 1
    if pk.startswith("0x"):
        pk = pk[2:]

    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        print(f"ERROR: cannot connect to {rpc}", file=sys.stderr)
        return 1

    account = w3.eth.account.from_key(pk)
    if account.address.lower() != wallet.lower():
        print(
            f"ERROR: PK derives address {account.address} but WALLET={wallet} "
            f"— the two must match.",
            file=sys.stderr,
        )
        return 1

    wallet_cs = Web3.to_checksum_address(wallet)
    matic = w3.eth.get_balance(wallet_cs)
    print(f"Wallet {wallet_cs} has {Web3.from_wei(matic, 'ether'):.4f} MATIC")
    if matic < Web3.to_wei(0.1, "ether"):
        print(
            "WARNING: < 0.1 MATIC — approvals may fail. Top up the wallet "
            "with at least ~1 MATIC for headroom.",
        )

    spenders = {
        "CTF Exchange": CTF_EXCHANGE,
        "Neg Risk Exchange": NEG_RISK_EXCHANGE,
        "Router": ROUTER,
    }

    usdce = w3.eth.contract(address=Web3.to_checksum_address(USDCE), abi=ERC20_ABI)
    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CONDITIONAL_TOKENS), abi=CTF_ABI
    )

    nonce = w3.eth.get_transaction_count(wallet_cs)

    def send(tx, label):
        nonlocal nonce
        tx["nonce"] = nonce
        tx["chainId"] = CHAIN_ID
        tx["gas"] = 200_000
        tx["maxFeePerGas"] = Web3.to_wei(200, "gwei")
        tx["maxPriorityFeePerGas"] = Web3.to_wei(40, "gwei")
        signed = w3.eth.account.sign_transaction(tx, pk)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  {label}: {h.hex()} ... ", end="", flush=True)
        receipt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        nonce += 1
        if receipt.status == 1:
            print("OK")
        else:
            print("FAILED")
        return receipt

    print("\n=== USDC.e approvals (ERC20 approve) ===")
    for label, spender in spenders.items():
        spender_cs = Web3.to_checksum_address(spender)
        allowance = usdce.functions.allowance(wallet_cs, spender_cs).call()
        if allowance >= HALF_MAX:
            print(f"  {label}: already approved (allowance={allowance})")
            continue
        tx = usdce.functions.approve(spender_cs, MAX_UINT256).build_transaction(
            {"from": wallet_cs}
        )
        send(tx, f"approve {label}")
        time.sleep(2)

    print("\n=== Conditional Tokens approvals (setApprovalForAll) ===")
    for label, spender in spenders.items():
        spender_cs = Web3.to_checksum_address(spender)
        if ctf.functions.isApprovedForAll(wallet_cs, spender_cs).call():
            print(f"  {label}: already approved")
            continue
        tx = ctf.functions.setApprovalForAll(spender_cs, True).build_transaction(
            {"from": wallet_cs}
        )
        send(tx, f"setApprovalForAll {label}")
        time.sleep(2)

    print("\nAll approvals set. You can now flip weather_live=true.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
