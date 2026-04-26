"""
Cluster copy-trade tracker.

Polls the 4-wallet operator cluster identified in analysis/report.md and
fires a (paper) copy-trade when a BUY matches the back-tested edge:

    price in [CLUSTER_MIN_PRICE, CLUSTER_MAX_PRICE]
    AND market is a crypto Up/Down/threshold daily
    AND size >= CLUSTER_MIN_USDC
    AND time-to-close in [CLUSTER_MIN_TTL_MIN, CLUSTER_MAX_TTL_MIN]

Detected trades are appended to data/cluster_paper_trades.jsonl. A
separate reconciliation step (analysis/14_cluster_reconcile.py — TBD)
should compute realised P&L once markets resolve.

Why a separate module instead of the existing watched_wallets path:
  - watched_wallets sizing relies on `deposits.amount`, which is empty
    for these externally-discovered wallets (they were never seen as
    suspect deposits).
  - The filters here are bespoke to the lottery-sniper edge, not the
    insider-trade thesis the main tracker is tuned for.
  - Keeps the existing tracker untouched while we evaluate this strategy.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone

import aiohttp

from config.settings import (
    CLUSTER_MAX_PRICE,
    CLUSTER_MAX_TTL_MIN,
    CLUSTER_MIN_PRICE,
    CLUSTER_MIN_TTL_MIN,
    CLUSTER_MIN_USDC,
    CLUSTER_OUR_MAX_USDC,
    CLUSTER_OUR_SIZE_RATIO,
    CLUSTER_POLL_INTERVAL_S,
    CLUSTER_TRACKER_ENABLED,
    CLUSTER_WALLETS,
)
from src.alerts.telegram import send_cluster_copy

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

LOG_PATH = os.path.join("data", "cluster_paper_trades.jsonl")

# Crypto assets we accept (matches the back-tested universe). Any other
# market kind is skipped — the edge was measured on these only.
CRYPTO_KEYWORDS = ("ethereum", "eth", "bitcoin", "btc", "solana", "sol", "xrp", "doge")

# Tag set so we can re-run on a tx without re-firing
_seen: dict[str, set[str]] = {w: set() for w in CLUSTER_WALLETS}

# Per-market end_date cache
_market_meta: dict[str, dict] = {}


async def _fetch_json(session: aiohttp.ClientSession, url: str, timeout_s: float = 10.0):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
        if r.status != 200:
            return None
        return await r.json()


async def _get_market(session: aiohttp.ClientSession, cid: str) -> dict | None:
    if cid in _market_meta:
        return _market_meta[cid]
    data = await _fetch_json(session, f"{CLOB_API}/markets/{cid}")
    if not data:
        return None
    raw = data.get("end_date_iso") or data.get("end_date")
    end = None
    if raw:
        try:
            end = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            end = None
    info = {
        "question":  data.get("question") or "",
        "end_date":  end,
        "tokens":    data.get("tokens") or [],
    }
    _market_meta[cid] = info
    return info


def _is_crypto(question: str) -> bool:
    q = (question or "").lower()
    return any(kw in q for kw in CRYPTO_KEYWORDS)


def _log_trade(record: dict) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


async def _poll_wallet(wallet: str, session: aiohttp.ClientSession) -> None:
    print(f"[cluster] tracking {wallet}")
    is_first = True
    while True:
        try:
            url = f"{DATA_API}/activity?user={wallet}&limit=50"
            trades = await _fetch_json(session, url, timeout_s=8.0)
            if isinstance(trades, list):
                # Process oldest -> newest within the page
                for t in sorted(trades, key=lambda e: e.get("timestamp", 0)):
                    tx = t.get("transactionHash") or ""
                    if not tx or tx in _seen[wallet]:
                        continue
                    _seen[wallet].add(tx)

                    # First poll bootstraps the dedup set silently.
                    if is_first:
                        continue

                    if t.get("type") != "TRADE" or t.get("side") != "BUY":
                        continue
                    try:
                        price = float(t.get("price", 0) or 0)
                        size_sh = float(t.get("size", 0) or 0)
                        usdc = float(t.get("usdcSize", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    # Some /activity records may omit usdcSize — derive it
                    if usdc <= 0 and price > 0 and size_sh > 0:
                        usdc = size_sh * price

                    cid = t.get("conditionId") or ""
                    if not cid:
                        continue

                    # Filter 1: price band
                    if not (CLUSTER_MIN_PRICE <= price <= CLUSTER_MAX_PRICE):
                        continue
                    # Filter 2: minimum size
                    if usdc < CLUSTER_MIN_USDC:
                        continue
                    # Filter 3: market kind (crypto only)
                    info = await _get_market(session, cid)
                    if not info:
                        continue
                    if not _is_crypto(info["question"]):
                        continue
                    # Filter 4: time-to-close window
                    end = info.get("end_date")
                    if not end:
                        continue
                    now = datetime.now(timezone.utc)
                    ttl_min = (end - now).total_seconds() / 60
                    if not (CLUSTER_MIN_TTL_MIN <= ttl_min <= CLUSTER_MAX_TTL_MIN):
                        continue

                    # All filters passed — synthesise our (paper) entry size
                    our_size = min(usdc * CLUSTER_OUR_SIZE_RATIO, CLUSTER_OUR_MAX_USDC)
                    record = {
                        "ts":             int(time.time()),
                        "iso_utc":        datetime.now(timezone.utc).isoformat(),
                        "trade_ts":       int(t.get("timestamp", 0)),
                        "tx_hash":        tx,
                        "wallet":         wallet,
                        "conditionId":    cid,
                        "question":       info["question"][:80],
                        "outcome":        t.get("outcome", ""),
                        "side":           t.get("side", ""),
                        "insider_price":  price,
                        "insider_size":   usdc,
                        "our_size":       round(our_size, 4),
                        "ttl_min":        round(ttl_min, 1),
                    }
                    _log_trade(record)
                    print(f"[cluster] COPY  {wallet[:10]}.. {info['question'][:48]!r}  "
                          f"{t.get('outcome'):<5} px={price:.3f} insider=${usdc:>6.0f} -> "
                          f"ours=${our_size:.2f} (ttl={ttl_min:.0f}m)")
                    try:
                        await send_cluster_copy(
                            wallet=wallet,
                            question=info["question"],
                            outcome=t.get("outcome", ""),
                            insider_price=price,
                            insider_size=usdc,
                            our_size=our_size,
                            ttl_min=ttl_min,
                        )
                    except Exception as e:
                        print(f"[cluster] telegram alert failed: {e}")
                if is_first:
                    print(f"[cluster] {wallet[:10]}.. bootstrapped {len(_seen[wallet])} historical txs")
                is_first = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[cluster] {wallet[:10]}.. poll error: {e}")
        await asyncio.sleep(CLUSTER_POLL_INTERVAL_S)


async def run_cluster_tracker() -> None:
    if not CLUSTER_TRACKER_ENABLED:
        print("[cluster] disabled (CLUSTER_TRACKER_ENABLED=false)")
        return
    print(f"[cluster] starting cluster copy-tracker for {len(CLUSTER_WALLETS)} wallets")
    print(f"[cluster] filters: price [{CLUSTER_MIN_PRICE}, {CLUSTER_MAX_PRICE}]  "
          f"size>=${CLUSTER_MIN_USDC}  ttl [{CLUSTER_MIN_TTL_MIN}, {CLUSTER_MAX_TTL_MIN}] min  "
          f"size_ratio={CLUSTER_OUR_SIZE_RATIO}")
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(_poll_wallet(w.lower(), session) for w in CLUSTER_WALLETS))
