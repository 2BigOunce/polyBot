# ─── SNIPPET À AJOUTER À LA FIN DE src/trading/arb_executor.py ───────────────
#
# Coller ce bloc AVANT la dernière ligne (si elle existe) ou à la fin du fichier.
# Dépendances déjà présentes dans arb_executor.py : asyncio, loop, client,
# order_lock, _get_client, _send_alert, SELL_PRICE, REAL_TRADING_ENABLED,
# OrderArgs, ARB_CAPITAL, ARB_MAX_SINGLE_TRADE.
# ──────────────────────────────────────────────────────────────────────────────

MAKER_TIMEOUT_SEC  = 300   # cancel unfilled orders after 5 minutes
MAKER_POLL_SEC     = 10    # check fill status every 10 seconds
MAKER_MAX_PRICE    = 0.35  # never place a MAKER buy above this price
MAKER_DEFAULT_LVLS = [0.05, 0.10, 0.20]  # fallback levels when book is empty


def _maker_levels(current_book: dict | None) -> list[float]:
    """
    Build dynamic price levels for MAKER limit buys.

    Logic:
      1. If the book has asks below MAKER_MAX_PRICE, use those prices
         (we anchor to existing market levels — likely to be filled quickly).
      2. Always add default fishing levels not already covered.
      3. Deduplicate and cap at 4 levels max.
    """
    levels = set()

    if current_book:
        for ask in current_book.get("asks", []):
            p = float(ask.get("price", 1.0))
            if p < MAKER_MAX_PRICE:
                levels.add(round(p, 3))

    for default in MAKER_DEFAULT_LVLS:
        if not any(abs(l - default) < 0.01 for l in levels):
            levels.add(default)

    return sorted(levels)[:4]


async def execute_maker_arb(
    token_id: str,
    question: str,
    winner: str,
    condition_id: str,
    current_book: dict | None,
    session,
) -> None:
    """
    MAKER arb strategy: place limit BUY orders at dynamic prices after resolution.

    Flow:
      1. Compute price levels from current book + defaults
      2. Place LIMIT BUY orders (with order_lock)
      3. Poll fills every MAKER_POLL_SEC (without lock)
      4. After MAKER_TIMEOUT_SEC, cancel unfilled orders (with lock)
      5. Sell acquired shares immediately (with lock)
      6. Alert Telegram
    """
    if not REAL_TRADING_ENABLED:
        levels = _maker_levels(current_book)
        await _send_alert(
            f"[MAKER dry-run]\n"
            f"Market: {question[:60]}\n"
            f"Winner: {winner}\n"
            f"Would place LIMIT BUY at: {levels}"
        )
        return

    loop   = asyncio.get_event_loop()
    client = _get_client()
    levels = _maker_levels(current_book)

    if not levels:
        return

    # ── Step 1: place LIMIT BUY orders ────────────────────────────────────────
    order_ids: list[tuple[str, float, float]] = []  # (order_id, price, size_shares)

    async with order_lock:
        for price in levels:
            budget = min(ARB_CAPITAL * (0.30 if price < 0.08 else 0.15), ARB_MAX_SINGLE_TRADE)
            size   = round(budget / price, 2)
            if size < 1.0:
                continue
            try:
                resp = await loop.run_in_executor(
                    None,
                    lambda p=price, s=size: client.create_and_post_order(
                        OrderArgs(token_id=token_id, price=p, size=s, side="BUY")
                    ),
                )
                status   = (resp or {}).get("status", "")
                order_id = (resp or {}).get("orderID") or (resp or {}).get("id", "")
                if status in ("live", "placed", "delayed") and order_id:
                    order_ids.append((order_id, price, size))
                    print(f"[arb] MAKER BUY placed @ {price} | {size:.0f} shares | {order_id[:12]}…")
                elif status == "matched":
                    # Instantly matched — treat as filled
                    print(f"[arb] MAKER BUY instantly matched @ {price}")
                else:
                    print(f"[arb] MAKER BUY rejected @ {price}: {resp}")
            except Exception as e:
                print(f"[arb] MAKER BUY error @ {price}: {e}")

    if not order_ids:
        print(f"[arb] MAKER: no orders placed for {condition_id[:14]}…")
        return

    # ── Step 2: poll for fills ─────────────────────────────────────────────────
    filled_shares = 0.0
    filled_cost   = 0.0
    open_orders   = list(order_ids)

    for _ in range(MAKER_TIMEOUT_SEC // MAKER_POLL_SEC):
        await asyncio.sleep(MAKER_POLL_SEC)
        still_open = []
        for order_id, price, size in open_orders:
            try:
                order = await loop.run_in_executor(
                    None,
                    lambda oid=order_id: client.get_order(oid),
                )
                if not order:
                    still_open.append((order_id, price, size))
                    continue
                matched = float(order.get("sizeMatched", 0) or 0)
                status  = order.get("status", "")
                if matched > 0 and status.upper() in ("MATCHED", "FILLED"):
                    filled_shares += matched
                    filled_cost   += matched * price
                    print(f"[arb] MAKER filled: {matched:.0f} shares @ {price}")
                elif status.upper() in ("LIVE", "DELAYED", "OPEN"):
                    still_open.append((order_id, price, size))
                # else: cancelled/expired — drop
            except Exception as e:
                print(f"[arb] MAKER poll error: {e}")
                still_open.append((order_id, price, size))

        open_orders = still_open
        if not open_orders:
            break  # all orders resolved

    # ── Step 3: cancel remaining orders ───────────────────────────────────────
    async with order_lock:
        for order_id, price, _ in open_orders:
            try:
                await loop.run_in_executor(
                    None,
                    lambda oid=order_id: client.cancel_order(oid),
                )
                print(f"[arb] MAKER cancelled unfilled @ {price}")
            except Exception as e:
                print(f"[arb] MAKER cancel error @ {price}: {e}")

        if filled_shares <= 0:
            print(f"[arb] MAKER: no fills — {condition_id[:14]}…")
            return

        # ── Step 4: sell acquired shares ──────────────────────────────────────
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
    profit = filled_shares * SELL_PRICE - filled_cost
    mult   = round(filled_shares * SELL_PRICE / filled_cost, 1) if filled_cost > 0 else 0
    await _send_alert(
        f"MAKER ARB EXECUTED\n"
        f"Market: {question[:60]}\n"
        f"Winner: {winner}\n"
        f"Bought: {filled_shares:.0f} shares | Cost: {filled_cost:.2f} USDC\n"
        f"Sell @ {SELL_PRICE} -> Revenue ~{filled_shares * SELL_PRICE:.2f} USDC\n"
        f"Profit ~{profit:.2f} USDC (x{mult})"
    )
