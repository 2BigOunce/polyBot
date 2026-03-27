# ─── SNIPPET À AJOUTER À LA FIN DE src/trading/arb_executor.py ───────────────
#
# Dépendances déjà présentes dans arb_executor.py : asyncio, loop, client,
# order_lock, _get_client, _send_alert, SELL_PRICE, REAL_TRADING_ENABLED,
# OrderArgs, ARB_CAPITAL, ARB_MAX_SINGLE_TRADE.
# ──────────────────────────────────────────────────────────────────────────────

MAKER_TIMEOUT_SEC = 300   # cancel unfilled order after 5 minutes
MAKER_POLL_SEC    = 10    # check fill status every 10 seconds
MAKER_MAX_PRICE   = 0.35  # never place a MAKER buy above this price
MAKER_DEFAULT_PRICE = 0.10  # fallback price when book is empty


def _maker_price(current_book: dict | None) -> float:
    """
    Choose a single dynamic price for the MAKER limit buy.

    Logic:
      - If the book has asks below MAKER_MAX_PRICE, use the lowest one
        (anchor to existing market — most likely to be filled).
      - Otherwise use MAKER_DEFAULT_PRICE as a fishing level.

    Single order avoids cannibalisation: on a CLOB, the highest bid is
    matched first. Multiple levels would cause our expensive orders to
    absorb market sells before our cheap ones get a chance.
    """
    if current_book:
        asks = [float(a["price"]) for a in current_book.get("asks", [])
                if float(a.get("price", 1.0)) < MAKER_MAX_PRICE]
        if asks:
            return round(min(asks), 3)

    return MAKER_DEFAULT_PRICE


async def execute_maker_arb(
    token_id: str,
    question: str,
    winner: str,
    condition_id: str,
    current_book: dict | None,
    session,
) -> None:
    """
    MAKER arb strategy: place a single dynamic LIMIT BUY after resolution.

    Flow:
      1. Pick price from current book (lowest ask < 0.35) or default 0.10
      2. Place one LIMIT BUY (with order_lock)
      3. Poll fills every MAKER_POLL_SEC (without lock)
      4. After MAKER_TIMEOUT_SEC, cancel if unfilled (with lock)
      5. If filled, sell immediately at SELL_PRICE (with lock)
      6. Alert Telegram
    """
    price = _maker_price(current_book)
    budget = min(ARB_CAPITAL * (0.30 if price < 0.08 else 0.15), ARB_MAX_SINGLE_TRADE)
    size   = round(budget / price, 2)

    if not REAL_TRADING_ENABLED:
        await _send_alert(
            f"[MAKER dry-run]\n"
            f"Market: {question[:60]}\n"
            f"Winner: {winner}\n"
            f"Would place LIMIT BUY: {size:.0f} shares @ {price}"
        )
        return

    if size < 1.0:
        return

    loop   = asyncio.get_event_loop()
    client = _get_client()

    # ── Step 1: place LIMIT BUY ────────────────────────────────────────────────
    order_id = None
    async with order_lock:
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: client.create_and_post_order(
                    OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
                ),
            )
            status   = (resp or {}).get("status", "")
            order_id = (resp or {}).get("orderID") or (resp or {}).get("id", "")
            if status in ("live", "placed", "delayed") and order_id:
                print(f"[arb] MAKER BUY placed @ {price} | {size:.0f} shares | {order_id[:12]}...")
            elif status == "matched":
                print(f"[arb] MAKER BUY instantly matched @ {price}")
                order_id = None  # already filled, skip polling
            else:
                print(f"[arb] MAKER BUY rejected @ {price}: {resp}")
                return
        except Exception as e:
            print(f"[arb] MAKER BUY error @ {price}: {e}")
            return

    # ── Step 2: poll for fill ─────────────────────────────────────────────────
    filled_shares = 0.0

    if order_id:
        for _ in range(MAKER_TIMEOUT_SEC // MAKER_POLL_SEC):
            await asyncio.sleep(MAKER_POLL_SEC)
            try:
                order   = await loop.run_in_executor(None, lambda: client.get_order(order_id))
                matched = float((order or {}).get("sizeMatched", 0) or 0)
                status  = (order or {}).get("status", "").upper()
                if matched > 0 and status in ("MATCHED", "FILLED"):
                    filled_shares = matched
                    print(f"[arb] MAKER filled: {matched:.0f} shares @ {price}")
                    break
                elif status not in ("LIVE", "DELAYED", "OPEN"):
                    break  # cancelled or expired
            except Exception as e:
                print(f"[arb] MAKER poll error: {e}")

        # ── Step 3: cancel if unfilled ─────────────────────────────────────────
        if filled_shares == 0:
            async with order_lock:
                try:
                    await loop.run_in_executor(None, lambda: client.cancel_order(order_id))
                    print(f"[arb] MAKER cancelled unfilled @ {price}")
                except Exception as e:
                    print(f"[arb] MAKER cancel error: {e}")
            return
    else:
        # Instantly matched — size was fully filled
        filled_shares = size

    # ── Step 4: sell acquired shares ──────────────────────────────────────────
    async with order_lock:
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: client.create_and_post_order(
                    OrderArgs(
                        token_id=token_id,
                        price=SELL_PRICE,
                        size=round(filled_shares, 2),
                        side="SELL",
                    )
                ),
            )
            print(f"[arb] MAKER SELL {filled_shares:.0f} shares @ {SELL_PRICE} | {(resp or {}).get('status')}")
        except Exception as e:
            print(f"[arb] MAKER SELL error: {e}")
            return

    # ── Step 5: alert ──────────────────────────────────────────────────────────
    cost   = filled_shares * price
    profit = filled_shares * SELL_PRICE - cost
    mult   = round(SELL_PRICE / price, 1)
    await _send_alert(
        f"MAKER ARB EXECUTED\n"
        f"Market: {question[:60]}\n"
        f"Winner: {winner}\n"
        f"Bought: {filled_shares:.0f} shares @ {price} | Cost: {cost:.2f} USDC\n"
        f"Sell @ {SELL_PRICE} -> Profit ~{profit:.2f} USDC (x{mult})"
    )
