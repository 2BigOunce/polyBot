import os
from dotenv import load_dotenv

load_dotenv()

# --- Connections ---
RPC_URL         = os.getenv("RPC_WS_URL")          # WebSocket — arb bot (low latency, Chainstack)
RPC_URL_TRACKER = os.getenv("RPC_WS_URL_TRACKER")  # WebSocket — tracker/deposits (PublicNode, high volume OK)
HTTP_RPC_URL    = os.getenv("RPC_HTTP_URL")         # HTTP — used for eth_getBlock calls

WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- Polygonscan ---
POLYGONSCAN_API_KEY = os.getenv("POLYGONSCAN_API_KEY")

# --- Polymarket CLOB ---
CLOB_API_KEY = os.getenv("CLOB_API_KEY")
CLOB_SECRET = os.getenv("CLOB_SECRET")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE")

# --- Polymarket contract addresses (Polygon mainnet) ---
# V1 (legacy, valid until 28 April 2026 ~11:00 UTC)
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"       # Bridged USDC (USDC.e), 6 decimals — legacy collateral
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"       # V1 main order-matching exchange
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"   # V1 NegRisk order-matching exchange

# V2 (active from 28 April 2026 ~11:00 UTC)
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"        # Polymarket USD, 1:1 backed by USDC — V2 collateral
COLLATERAL_ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"   # wraps USDC.e → pUSD via wrap(asset, to, amount)
CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"     # V2 main exchange
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"  # V2 NegRisk exchange

# Unchanged across V1 → V2 (ERC-1155 conditional tokens)
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"    # NegRisk adapter — ERC-1155 for neg_risk markets
GNOSIS_CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"          # Gnosis ConditionalTokens — ERC-1155 for standard markets

# Contracts that can receive USDC.e deposits (filter for the deposit monitor Transfer subscription).
# Pre-V2: users deposit USDC.e directly to the V1 exchanges.
# Post-V2: users must wrap USDC.e → pUSD through CollateralOnramp first, so ONRAMP becomes the
# choke point of incoming capital. V1 exchange addresses are kept (harmless — no more events after
# cutover) and V2 exchange addresses are included in case a contract path lets USDC.e land there
# directly during the migration window.
POLYMARKET_CONTRACTS = [
    CTF_EXCHANGE, NEG_RISK_EXCHANGE,           # V1 exchanges (go silent post-cutover)
    CTF_EXCHANGE_V2, NEG_RISK_EXCHANGE_V2,     # V2 exchanges
    COLLATERAL_ONRAMP,                         # V2 wrap choke point — catches all new capital
    NEG_RISK_ADAPTER,
]

# --- Detection thresholds ---

ALERT_DEPOSIT_HIGH = 15_000   # USDC — wallet < 1 day old
ALERT_DEPOSIT_MEDIUM = 8_000  # USDC — wallet < 5 days old
ALERT_DEPOSIT_SUPER = 3_000   # USDC — wallet < 5 days old — super alert only, no copy trade
WALLET_AGE_HIGH = 1           # days
WALLET_AGE_MEDIUM = 5         # days

MIN_INSIDER_TRADE_SIZE = 1_000  # USDC — ignore insider BUYs below this size
MIN_INSIDER_PRICE      = 0.10  # ignore BUYs at near-zero odds (arb/dust)
MAX_INSIDER_PRICE      = 0.75  # ignore BUYs at near-certainty odds (arb/MM)

CLUSTER_MIN_SIZE = 3          # min suspects sharing a funder to trigger cluster alert
CEX_FUNDER_THRESHOLD = 50     # funders with more successors than this are treated as CEX/bridge and ignored

# --- Behavioral filter (Option B) ---
WALLET_INACTIVITY_HOURS = 24  # remove from watchlist if no trade within this delay
EARLY_SELL_HOURS = 24         # flag as unreliable if wallet sells with > N hours before market resolution

# --- Paper trading ---
PAPER_BANKROLL = 1_000        # USDC — total simulated capital
PAPER_MAX_TRADE = 100         # USDC — cap per individual trade
PAPER_MAX_MARKET_EXPOSURE = 200  # USDC — cap total per market across all buys

# --- Real trading ---
CLOB_PROXY_URL          = os.getenv("CLOB_PROXY_URL")  # residential proxy, e.g. http://user:pass@host:port
REAL_TRADING_ENABLED    = os.getenv("REAL_TRADING_ENABLED", "false").lower() == "true"
REAL_WALLET_PRIVATE_KEY  = os.getenv("REAL_WALLET_PRIVATE_KEY")
REAL_PROXY_WALLET        = os.getenv("REAL_PROXY_WALLET")  # Polymarket proxy wallet address (maker in orders)
REAL_MAX_TRADE          = float(os.getenv("REAL_MAX_TRADE", "10"))    # USDC cap per trade
REAL_MAX_POSITION_PCT   = float(os.getenv("REAL_MAX_POSITION_PCT", "0.10"))  # 10% of balance max
REAL_MAX_PRICE_DRIFT        = float(os.getenv("REAL_MAX_PRICE_DRIFT", "0.15"))       # skip if price moved >15%
REAL_MIN_LIQUIDITY          = float(os.getenv("REAL_MIN_LIQUIDITY", "500"))           # USDC — min ask-side volume before placing order
REAL_MAX_MARKET_EXPOSURE    = float(os.getenv("REAL_MAX_MARKET_EXPOSURE", "50"))      # USDC — max total open per market/outcome across all wallets
REAL_MAX_SPORTS_EXPOSURE_PCT = float(os.getenv("REAL_MAX_SPORTS_EXPOSURE_PCT", "0.20")) # max % of balance open on sports markets
REAL_MIN_EDGE               = float(os.getenv("REAL_MIN_EDGE", "0.02"))               # minimum p̂ - price edge required to enter a trade
REAL_SPORTS_MIN_WINRATE     = float(os.getenv("REAL_SPORTS_MIN_WINRATE", "0.60"))     # sports wallets: min historical win rate (if >= min trades)
REAL_SPORTS_MIN_PHAT        = float(os.getenv("REAL_SPORTS_MIN_PHAT", "0.60"))        # sports wallets: min scorer confidence (fallback if not enough history)
REAL_SPORTS_MIN_TRADES      = int(os.getenv("REAL_SPORTS_MIN_TRADES", "10"))          # min resolved sports trades to use WR filter

# --- Insider entry filter ---
INSIDER_MAX_MARKETS    = int(os.getenv("INSIDER_MAX_MARKETS", "3"))     # reject if wallet traded > N distinct markets (90d)
INSIDER_MAX_SPORTS_PCT = float(os.getenv("INSIDER_MAX_SPORTS_PCT", "0.30"))  # reject if > 30% of trades are sports
INSIDER_PENDING_TRADES = int(os.getenv("INSIDER_PENDING_TRADES", "3"))  # alert when a 0-history wallet reaches N trades

# --- Resolution arbitrage (Phase 6) ---
ARB_MAX_STALE_PRICE  = float(os.getenv("ARB_MAX_STALE_PRICE", "0.90"))   # buy stale asks below this price
ARB_CAPITAL          = float(os.getenv("ARB_CAPITAL", "2000"))            # USDC reserved for arb bot (total budget per resolution)

# --- CLOB mispricing arbitrage ---
# Buy asks priced well below the current best bid, resell immediately at bid.
# Both conditions must hold: ratio AND absolute spread.
CLOB_MISPRICING_ENABLED    = os.getenv("CLOB_MISPRICING_ENABLED", "false").lower() == "true"
CLOB_MISPRICING_BID_RATIO  = float(os.getenv("CLOB_MISPRICING_BID_RATIO",  "0.90"))  # ask < best_bid * this
CLOB_MISPRICING_MIN_SPREAD = float(os.getenv("CLOB_MISPRICING_MIN_SPREAD", "0.05"))  # ask < best_bid - this
CLOB_MISPRICING_MAX_TRADE  = float(os.getenv("CLOB_MISPRICING_MAX_TRADE",  "50"))    # hard cap per trade (USDC)
CLOB_MISPRICING_MIN_BID_DEPTH = float(os.getenv("CLOB_MISPRICING_MIN_BID_DEPTH", "50"))  # skip if bid_size * bid_price < this (anti-spoofing)

# --- MAKER pre-positioning strategy ---
# Place resting BUY orders at a low price on both outcomes of low-liquidity markets.
# Profits when a counterparty triggers the CLOB MINT mechanism near resolution.
MAKER_POSITIONING_ENABLED  = os.getenv("MAKER_POSITIONING_ENABLED", "false").lower() == "true"
MAKER_MAX_MARKETS          = int(os.getenv("MAKER_MAX_MARKETS", "6"))          # max concurrent markets
MAKER_SIZE_PER_MARKET      = float(os.getenv("MAKER_SIZE_PER_MARKET", "150"))  # USDC per market (split across 2 sides)
MAKER_ORDER_PRICE          = float(os.getenv("MAKER_ORDER_PRICE", "0.05"))     # limit price per token
MAKER_CANCEL_TIMEOUT_DAYS  = int(os.getenv("MAKER_CANCEL_TIMEOUT_DAYS", "3")) # cancel unfilled orders after N days
MAKER_MAX_TOTAL_EXPOSURE   = float(os.getenv("MAKER_MAX_TOTAL_EXPOSURE", "2000"))  # USDC cap across all markets
MAKER_MAX_VOLUME           = float(os.getenv("MAKER_MAX_VOLUME", "100000"))    # skip markets with volume > this
MAKER_MAX_DAYS_TO_CLOSE    = int(os.getenv("MAKER_MAX_DAYS_TO_CLOSE", "5"))    # skip markets closing after this many days
MAKER_SELL_PRICE           = float(os.getenv("MAKER_SELL_PRICE", "0.90"))      # limit sell fallback if no good bid
MAKER_MIN_SELL_BID         = float(os.getenv("MAKER_MIN_SELL_BID", "0.50"))    # min bid to trigger immediate taker sell

# Sandwich arb: fire when ask < MAX_ASK and bid > MIN_BID simultaneously
MAKER_SANDWICH_MAX_ASK     = float(os.getenv("MAKER_SANDWICH_MAX_ASK",    "0.20"))  # max ask price to buy
MAKER_SANDWICH_MIN_BID     = float(os.getenv("MAKER_SANDWICH_MIN_BID",    "0.80"))  # min bid price to sell into
MAKER_SANDWICH_MIN_PROFIT  = float(os.getenv("MAKER_SANDWICH_MIN_PROFIT", "0.50"))  # min bid-ask spread
MAKER_SANDWICH_MAX_TRADE   = float(os.getenv("MAKER_SANDWICH_MAX_TRADE",  "100"))   # USDC cap per trade

# --- Cluster copy-trade (lottery-sniper operator) ---
# Tracks the 4-wallet cluster identified in analysis/report.md (operator
# behind 0x06dc51). Copies their MID-BOOK entries (price 0.10-0.30, ttl
# 1-4h before close, size >= $10) which back-tested at +$371/trade with
# 68% win rate over ~30 days. Excludes the lottery sweeps at $0.001
# (impossible to copy due to latency).
#
# In paper mode the tracker only logs candidate trades to a JSONL file.
# A separate reconciliation job (TBD) will compute realised P&L.
CLUSTER_TRACKER_ENABLED   = os.getenv("CLUSTER_TRACKER_ENABLED", "true").lower() == "true"
CLUSTER_POLL_INTERVAL_S   = int(os.getenv("CLUSTER_POLL_INTERVAL_S", "10"))   # per-wallet poll
CLUSTER_MIN_PRICE         = float(os.getenv("CLUSTER_MIN_PRICE", "0.10"))    # entry price floor
CLUSTER_MAX_PRICE         = float(os.getenv("CLUSTER_MAX_PRICE", "0.30"))    # entry price ceiling
CLUSTER_MIN_USDC          = float(os.getenv("CLUSTER_MIN_USDC", "10"))       # ignore spam
CLUSTER_MIN_TTL_MIN       = float(os.getenv("CLUSTER_MIN_TTL_MIN", "60"))    # 1h
CLUSTER_MAX_TTL_MIN       = float(os.getenv("CLUSTER_MAX_TTL_MIN", "240"))   # 4h
CLUSTER_OUR_SIZE_RATIO    = float(os.getenv("CLUSTER_OUR_SIZE_RATIO", "0.125"))  # 1/8 of insider size
CLUSTER_OUR_MAX_USDC      = float(os.getenv("CLUSTER_OUR_MAX_USDC", "100"))  # hard per-trade cap (paper)
# The operator cluster — established in analysis/report.md section 7.A.
CLUSTER_WALLETS = [
    "0x06dc51826bc524d9a83770e7de9dd7e005b04524",  # ETH/SOL/XRP daily markets
    "0x04283f2fef49d70d8c55ab240450d17a65bf85b1",  # 5-min Up/Down markets
    "0x6e1d5040d0ac73709b0621f620d2a60b80d2d0fa",  # BTC daily/range markets
]
