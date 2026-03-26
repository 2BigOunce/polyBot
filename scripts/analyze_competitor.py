"""
Competitor bot analysis — post-resolution arbitrage on Polymarket
Wallet: 0x99558e8d51b707069ddd64e8583c699bda94e3e6
"""

import json
import urllib.request
import urllib.error

RPC = "https://polygon-bor-rpc.publicnode.com"

TX_HASHES = [
    "0x9b195a019a80167fba688237e52316013cf5b9ee53aaf7cf6341c9a94a931825",
    "0xa52c65f882075a7b885aa89b68b41c090d1ccc02a4bf200e19055578731b2bc1",
    "0xc7a69550612ec69f5a0e16104d24853d5b0f72d93426281f4b78acccfbc4f8d3",
    "0xfbe19dc94726bf1b3644fae64bbc52129eefcfda2e0068392ad5b5639564172f",
    "0x7568079096d9c81d0ea25727c3ab4febc824e845a2d4c55c27737c63e4c6c767",
    "0x3b77a7343c29232713684ebe93ecaccf8ccb6fbdbb08f48c2cc9578a8d861aa5",
]

ORDER_FILLED_TOPIC = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
CONDITION_RESOLUTION_TOPIC = "0x00000000000000000000000000000000000000000000000000000000bf8bd2fb"
# Correct topic for ConditionResolution
CONDITION_RESOLUTION_TOPIC0 = "0xbf8bd2fbba2704e77399bcf761d7c27391b79d39c4c9d48af0bfce3bb3f0b23"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"


def rpc_call(method, params, call_id=1):
    payload = json.dumps({"jsonrpc": "2.0", "id": call_id, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=payload, headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; polyBot-analyzer/1.0)",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_receipt(tx_hash):
    r = rpc_call("eth_getTransactionReceipt", [tx_hash])
    return r.get("result")


def get_block(block_hex, full=False):
    r = rpc_call("eth_getBlockByNumber", [block_hex, full])
    return r.get("result")


def get_logs(from_block_hex, to_block_hex, address, topics):
    params = [{
        "fromBlock": from_block_hex,
        "toBlock": to_block_hex,
        "address": address,
        "topics": topics,
    }]
    r = rpc_call("eth_getLogs", params)
    return r.get("result", [])


def parse_order_filled(log):
    """Parse an OrderFilled event log."""
    data = log["data"]
    if data.startswith("0x"):
        data = data[2:]
    # data = makerAssetId(32b) + takerAssetId(32b) + makerAmountFilled(32b) + takerAmountFilled(32b) + fee(32b)
    # Each field = 64 hex chars
    if len(data) < 64 * 5:
        return None
    maker_asset_id = int(data[0:64], 16)
    taker_asset_id = int(data[64:128], 16)
    maker_amount = int(data[128:192], 16)
    taker_amount = int(data[192:256], 16)
    fee = int(data[256:320], 16)

    # USDC is asset_id == 0 (it's USDC collateral)
    # If makerAssetId == 0 → maker is selling USDC, buying shares → makerAmountFilled is USDC spent
    # If takerAssetId == 0 → taker is receiving USDC → takerAmountFilled is USDC
    # For a BUY: wallet buys shares → pays USDC → makerAssetId = token_id (shares), takerAssetId = 0
    # Actually in CTF exchange:
    #   makerAssetId = 0 means maker gives USDC
    #   takerAssetId = 0 means taker gives USDC
    if maker_asset_id != 0:
        usdc_amount = taker_amount / 1e6
        token_id = maker_asset_id
        side = "BUY_SHARES"  # maker has shares, taker pays USDC
    else:
        usdc_amount = maker_amount / 1e6
        token_id = taker_asset_id
        side = "SELL_SHARES"  # maker pays USDC, taker has shares

    # topics[1] = orderHash, topics[2] = maker, topics[3] = taker
    topics = log.get("topics", [])
    maker = "0x" + topics[2][26:] if len(topics) > 2 else None
    taker = "0x" + topics[3][26:] if len(topics) > 3 else None

    return {
        "usdc_amount": usdc_amount,
        "token_id": token_id,
        "maker_asset_id": maker_asset_id,
        "taker_asset_id": taker_asset_id,
        "maker_amount": maker_amount / 1e6,
        "taker_amount": taker_amount / 1e6,
        "side": side,
        "maker": maker,
        "taker": taker,
    }


def find_condition_resolution(block_number_int, window=500):
    """Search for ConditionResolution events in the window before the trade block."""
    from_block = max(0, block_number_int - window)
    to_block = block_number_int

    from_hex = hex(from_block)
    to_hex = hex(to_block)

    logs = get_logs(from_hex, to_hex, CTF_CONTRACT, [CONDITION_RESOLUTION_TOPIC0])
    return logs


def analyze():
    print("=" * 80)
    print("COMPETITOR BOT ANALYSIS — Post-resolution arbitrage")
    print(f"Wallet: 0x99558e8d51b707069ddd64e8583c699bda94e3e6")
    print("=" * 80)

    results = []

    for i, tx_hash in enumerate(TX_HASHES, 1):
        print(f"\n[TX {i}/6] {tx_hash[:20]}...")

        # Step 1: Get receipt
        receipt = get_receipt(tx_hash)
        if not receipt:
            print(f"  ERROR: no receipt found")
            continue

        block_hex = receipt["blockNumber"]
        block_number = int(block_hex, 16)
        print(f"  Block: {block_number} ({block_hex})")

        # Find OrderFilled log
        order_filled = None
        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if topics and topics[0].lower() == ORDER_FILLED_TOPIC.lower():
                order_filled = parse_order_filled(log)
                print(f"  OrderFilled found: {order_filled}")
                break

        if not order_filled:
            print(f"  WARNING: no OrderFilled event in this TX")
            # Print all topics to debug
            for log in receipt.get("logs", []):
                topics = log.get("topics", [])
                if topics:
                    print(f"    log topic0: {topics[0]}")
            continue

        # Get trade block timestamp
        trade_block = get_block(block_hex)
        trade_ts = int(trade_block["timestamp"], 16) if trade_block else None
        print(f"  Trade timestamp: {trade_ts}")

        # Step 2: Search for ConditionResolution before this trade
        print(f"  Searching ConditionResolution in blocks [{block_number-500}, {block_number}]...")
        resolution_logs = find_condition_resolution(block_number, window=500)
        print(f"  Found {len(resolution_logs)} ConditionResolution events")

        if not resolution_logs:
            # Try wider window
            print(f"  Trying wider window (2000 blocks)...")
            resolution_logs = find_condition_resolution(block_number, window=2000)
            print(f"  Found {len(resolution_logs)} ConditionResolution events (wide)")

        # Find the one closest to (and before) the trade
        best_resolution = None
        best_delta_blocks = None

        for rlog in resolution_logs:
            res_block_hex = rlog["blockNumber"]
            res_block_num = int(res_block_hex, 16)
            delta = block_number - res_block_num

            if delta >= 0:  # resolution must be BEFORE or AT trade
                if best_delta_blocks is None or delta < best_delta_blocks:
                    best_delta_blocks = delta
                    best_resolution = rlog
                    best_res_block_num = res_block_num
                    best_res_block_hex = res_block_hex

        if best_resolution:
            # Get resolution block timestamp
            res_block = get_block(best_res_block_hex)
            res_ts = int(res_block["timestamp"], 16) if res_block else None
            delta_seconds = (trade_ts - res_ts) if (trade_ts and res_ts) else None

            print(f"  Resolution block: {best_res_block_num}, trade block: {block_number}")
            print(f"  Delta blocks: {best_delta_blocks}, delta seconds: {delta_seconds}")

            # Decode condition from resolution log data
            # ConditionResolution(address oracle, bytes32 questionId, bytes32 conditionId, uint256 outcomeSlotCount, uint256[] payoutNumerators)
            res_data = best_resolution.get("data", "")
            res_topics = best_resolution.get("topics", [])
            # topics[1] = conditionId (indexed)
            condition_id = res_topics[1] if len(res_topics) > 1 else None
            print(f"  ConditionId: {condition_id}")

            # Shares and price estimation
            # For post-resolution arb: bot buys at price P, redeems at 1.0
            # We need shares count and price
            # shares = taker_amount (if maker sells shares) or maker_amount
            # usdc = the other side
            of = order_filled
            if of["maker_asset_id"] != 0:
                # maker has shares (token_id), taker has USDC
                shares = of["maker_amount"]  # shares given by maker
                usdc_paid = of["taker_amount"]  # USDC paid by taker (our wallet)
            else:
                # maker has USDC, taker has shares
                shares = of["taker_amount"]
                usdc_paid = of["maker_amount"]

            if shares > 0:
                price = usdc_paid / shares
            else:
                price = None

            profit = shares * (1.0 - price) if price is not None else None

            results.append({
                "tx": tx_hash,
                "tx_short": tx_hash[:10] + "...",
                "block_trade": block_number,
                "block_resolution": best_res_block_num,
                "delta_blocks": best_delta_blocks,
                "delta_seconds": delta_seconds,
                "trade_ts": trade_ts,
                "usdc_paid": usdc_paid,
                "shares": shares,
                "price": price,
                "profit": profit,
                "condition_id": condition_id,
            })
        else:
            print(f"  WARNING: no ConditionResolution found for this trade")
            # Still record partial result
            of = order_filled
            if of["maker_asset_id"] != 0:
                shares = of["maker_amount"]
                usdc_paid = of["taker_amount"]
            else:
                shares = of["taker_amount"]
                usdc_paid = of["maker_amount"]
            price = usdc_paid / shares if shares > 0 else None
            results.append({
                "tx": tx_hash,
                "tx_short": tx_hash[:10] + "...",
                "block_trade": block_number,
                "block_resolution": None,
                "delta_blocks": None,
                "delta_seconds": None,
                "trade_ts": trade_ts,
                "usdc_paid": usdc_paid,
                "shares": shares,
                "price": price,
                "profit": shares * (1.0 - price) if price else None,
                "condition_id": None,
            })

    # Step 5: Report
    print("\n")
    print("=" * 80)
    print("RAPPORT FINAL")
    print("=" * 80)

    import datetime

    header = f"{'TX':12} | {'Date (UTC)':20} | {'USDC':>10} | {'Prix':>8} | {'Shares':>10} | {'Profit est.':>12} | {'Δ blocs':>8} | {'Δ sec':>8}"
    print(header)
    print("-" * len(header))

    total_profit = 0
    deltas_blocks = []
    deltas_seconds = []

    for r in results:
        date_str = datetime.datetime.utcfromtimestamp(r["trade_ts"]).strftime("%Y-%m-%d %H:%M:%S") if r["trade_ts"] else "N/A"
        usdc_str = f"{r['usdc_paid']:.2f}" if r["usdc_paid"] else "N/A"
        price_str = f"{r['price']:.4f}" if r["price"] else "N/A"
        shares_str = f"{r['shares']:.2f}" if r["shares"] else "N/A"
        profit_str = f"{r['profit']:.2f}" if r["profit"] else "N/A"
        db_str = str(r["delta_blocks"]) if r["delta_blocks"] is not None else "N/A"
        ds_str = str(r["delta_seconds"]) if r["delta_seconds"] is not None else "N/A"

        print(f"{r['tx_short']:12} | {date_str:20} | {usdc_str:>10} | {price_str:>8} | {shares_str:>10} | {profit_str:>12} | {db_str:>8} | {ds_str:>8}")

        if r["profit"]:
            total_profit += r["profit"]
        if r["delta_blocks"] is not None:
            deltas_blocks.append(r["delta_blocks"])
        if r["delta_seconds"] is not None:
            deltas_seconds.append(r["delta_seconds"])

    print()
    print("RÉSUMÉ")
    print(f"  Profit total estimé : {total_profit:.2f} USDC")

    if deltas_blocks:
        print(f"  Latence en blocs  : moy={sum(deltas_blocks)/len(deltas_blocks):.1f}  min={min(deltas_blocks)}  max={max(deltas_blocks)}")
    if deltas_seconds:
        print(f"  Latence en secondes: moy={sum(deltas_seconds)/len(deltas_seconds):.1f}  min={min(deltas_seconds)}  max={max(deltas_seconds)}")

    if deltas_seconds:
        avg = sum(deltas_seconds) / len(deltas_seconds)
        if avg < 10:
            verdict = "CONCURRENT SERIEUX (< 10s) — bot très rapide, potentiellement co-localisé"
        elif avg < 30:
            verdict = "CONCURRENT MODERE (10-30s) — bot rapide mais battable avec optimisations"
        else:
            verdict = "BOT LENT (> 30s) — facile à battre avec une bonne implémentation"
        print(f"\n  VERDICT: {verdict}")
    else:
        print("\n  VERDICT: Impossible à évaluer — résolutions non trouvées dans la fenêtre de 2000 blocs")
        print("  => Possibilité que les résolutions soient bien avant les trades (window trop petite)")
        print("     ou que les trades ne soient pas de l'arb post-résolution")

    print()
    print("Détail par TX (condition_id):")
    for r in results:
        print(f"  {r['tx'][:20]}... → condition_id={r['condition_id']} | price={r['price']:.4f if r['price'] else 'N/A'} | Δ={r['delta_seconds']}s")


if __name__ == "__main__":
    analyze()
