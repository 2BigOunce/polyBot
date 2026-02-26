import os
from dotenv import load_dotenv

load_dotenv()

# --- Connections ---
RPC_URL      = os.getenv("RPC_WS_URL")   # WebSocket — used for eth_subscribe
HTTP_RPC_URL = os.getenv("RPC_HTTP_URL") # HTTP — used for eth_getBlock calls

WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- Polymarket CLOB ---
CLOB_API_KEY = os.getenv("CLOB_API_KEY")
CLOB_SECRET = os.getenv("CLOB_SECRET")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE")

# --- Polymarket contract addresses (Polygon mainnet) ---
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"       # Bridged USDC (USDC.e), 6 decimals
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"       # Main exchange contract
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"   # NegRisk exchange
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"    # NegRisk adapter

# All contracts that can receive USDC deposits — we monitor transfers to any of these
POLYMARKET_CONTRACTS = [CTF_EXCHANGE, NEG_RISK_EXCHANGE, NEG_RISK_ADAPTER]

# --- Detection thresholds ---

ALERT_DEPOSIT_HIGH = 1_000    # USDC — wallet < 1 day old
ALERT_DEPOSIT_MEDIUM = 5_000  # USDC — wallet < 7 days old
WALLET_AGE_HIGH = 1           # days
WALLET_AGE_MEDIUM = 7         # days
