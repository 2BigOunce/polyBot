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
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"       # Bridged USDC (USDC.e), 6 decimals
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"       # Main order-matching exchange
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"   # NegRisk order-matching exchange
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"    # NegRisk adapter — ERC-1155 tokens for neg_risk markets
GNOSIS_CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"          # Gnosis ConditionalTokens — ERC-1155 tokens for standard markets

# All contracts that can receive USDC deposits — we monitor transfers to any of these
POLYMARKET_CONTRACTS = [CTF_EXCHANGE, NEG_RISK_EXCHANGE, NEG_RISK_ADAPTER]

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
ARB_CAPITAL          = float(os.getenv("ARB_CAPITAL", "2000"))            # USDC reserved for arb bot
ARB_MAX_SINGLE_TRADE = float(os.getenv("ARB_MAX_SINGLE_TRADE", "50"))    # hard cap per arb execution (safety)

# --- CLOB mispricing arbitrage ---
# Buy asks priced well below the current best bid, resell immediately at bid.
# Both conditions must hold: ratio AND absolute spread.
CLOB_MISPRICING_ENABLED    = os.getenv("CLOB_MISPRICING_ENABLED", "false").lower() == "true"
CLOB_MISPRICING_BID_RATIO  = float(os.getenv("CLOB_MISPRICING_BID_RATIO",  "0.80"))  # ask < best_bid * this
CLOB_MISPRICING_MIN_SPREAD = float(os.getenv("CLOB_MISPRICING_MIN_SPREAD", "0.10"))  # ask < best_bid - this
CLOB_MISPRICING_MAX_TRADE  = float(os.getenv("CLOB_MISPRICING_MAX_TRADE",  "50"))    # hard cap per trade (USDC)
