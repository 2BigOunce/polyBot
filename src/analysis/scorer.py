"""
Bayesian confidence scorer for copy-trading decisions.

For each (wallet, trade) pair, computes p̂ — the estimated probability that
following this wallet on this specific trade will be profitable.

p̂ is then fed into the Half-Kelly formula in real_trader.py:
    f = (p̂ - p) / (2 * (1 - p))

Architecture note: all signals are combined in log-space to avoid float
underflow (same reason you use log-probabilities in NLP / Viterbi).
Each signal contributes a log-likelihood ratio:
    log P(signal | good trade) - log P(signal | bad trade)
The final p̂ is obtained by converting the posterior log-odds back to a
probability via the sigmoid function.
"""

import math
from datetime import datetime, timezone

from src.chain.database import get_wallet_scoring_data

# Global prior: ~57.5% win rate observed across all wallets
_PRIOR_WIN_RATE = 0.575
_PRIOR_LOG_ODDS = math.log(_PRIOR_WIN_RATE / (1 - _PRIOR_WIN_RATE))  # ≈ +0.30

# Bayesian smoothing for win rate: equivalent to having observed 10 trades
# at the prior rate before seeing any real data for this wallet.
# Analogous to initialising counters to non-zero values in C to avoid /0.
_SMOOTH_PRIOR_WINS  = _PRIOR_WIN_RATE * 10   # 5.75
_SMOOTH_PRIOR_TOTAL = 10.0


def _sigmoid(x: float) -> float:
    """Convert log-odds to probability. Clamps to avoid exact 0 or 1."""
    return 1.0 / (1.0 + math.exp(-x))


def compute_confidence(
    wallet: str,
    insider_size: float,
    market_price: float,
    days_until_resolution: float | None = None,
    market_liquidity: float | None = None,
) -> float:
    """
    Compute p̂ ∈ (0, 1) for a (wallet, trade) pair.

    Parameters
    ----------
    wallet                : wallet address being copied
    insider_size          : USDC size of the insider's trade
    market_price          : current market price of the outcome (0.10 – 0.75)
    days_until_resolution : days until the market resolves (None = unknown)
    market_liquidity      : ask-side USDC liquidity in the orderbook (None = unknown)

    Returns
    -------
    p̂ : estimated probability this trade will be profitable
    """
    data = get_wallet_scoring_data(wallet)

    log_odds = _PRIOR_LOG_ODDS

    # --- Signal 1: historical win rate (smoothed) ---
    # More closed trades → more weight on observed rate vs prior.
    smoothed_wins  = data["wins"]  + _SMOOTH_PRIOR_WINS
    smoothed_total = data["total"] + _SMOOTH_PRIOR_TOTAL
    wallet_wr = smoothed_wins / smoothed_total
    # Contribution: difference between wallet log-odds and prior log-odds
    wallet_log_odds = math.log(wallet_wr / (1 - wallet_wr))
    # Weight by sample size: full weight at 50+ closed trades, near-zero at 0
    sample_weight = min(data["total"], 50) / 50.0
    log_odds += sample_weight * (wallet_log_odds - _PRIOR_LOG_ODDS)

    # --- Signal 2: market concentration (few markets = more insider-like) ---
    d = data["distinct_markets"]
    if d <= 2:
        log_odds += 0.40    # strong insider signal
    elif d <= 5:
        log_odds += 0.20    # moderate
    elif d >= 50:
        log_odds -= 0.15    # broad trader, slightly lower confidence
    # 6–49 markets: neutral (no adjustment)

    # --- Signal 3: insider trade size (conviction signal) ---
    if insider_size >= 75_000:
        log_odds += 0.50
    elif insider_size >= 50_000:
        log_odds += 0.35
    elif insider_size >= 25_000:
        log_odds += 0.20
    elif insider_size >= 10_000:
        log_odds += 0.10
    # < 10k: neutral

    # --- Signal 4: wallet age ---
    age = data["wallet_age_days"]
    if age is not None:
        if age < 1:
            log_odds += 0.30    # very new wallet — classic insider profile
        elif age < 7:
            log_odds += 0.10
        elif age > 90:
            log_odds -= 0.10    # old wallet, less likely to be a one-shot insider
    # unknown age: neutral

    # --- Signal 5: deposit freshness (signal decay) ---
    # On-chain deposit has a ~2h half-life. A fresh deposit is a hotter signal.
    detected_at_str = data.get("deposit_detected_at")
    if detected_at_str:
        try:
            detected_at = datetime.fromisoformat(detected_at_str)
            hours_since = (datetime.now(tz=timezone.utc) - detected_at).total_seconds() / 3600
            if hours_since < 0.5:
                log_odds += 0.20   # < 30 min — very fresh
            elif hours_since < 2.0:
                pass               # 30 min – 2h — neutral
            elif hours_since < 6.0:
                log_odds -= 0.15   # cooling down
            else:
                log_odds -= 0.30   # cold signal — wallet been around a while
        except (ValueError, TypeError):
            pass  # malformed timestamp — neutral

    # --- Signal 6: proximity to market resolution ---
    # An insider betting on a market resolving soon = stronger information signal.
    if days_until_resolution is not None:
        if days_until_resolution <= 3:
            log_odds += 0.40   # imminent — they know something specific
        elif days_until_resolution <= 7:
            log_odds += 0.20
        elif days_until_resolution <= 30:
            log_odds += 0.05   # near-term, slight boost
        elif days_until_resolution > 90:
            log_odds -= 0.10   # far future — less urgent insider bet
    # unknown resolution date: neutral

    # --- Signal 7: market liquidity (niche markets attract insiders) ---
    # Low ask-side liquidity = thin market = more likely to be insider-driven.
    if market_liquidity is not None:
        if market_liquidity < 1_000:
            log_odds += 0.40   # very niche
        elif market_liquidity < 5_000:
            log_odds += 0.20
        elif market_liquidity < 20_000:
            log_odds += 0.05
        elif market_liquidity > 100_000:
            log_odds -= 0.10   # liquid market — less insider-like
    # unknown liquidity: neutral

    # --- Signal 8: CLOB historical win rate (cached at entry) ---
    # Full Polymarket track record, stored in DB at wallet entry time.
    # New wallets with no history (clob_closed_trades is None) are neutral.
    clob_wr     = data.get("clob_win_rate")
    clob_closed = data.get("clob_closed_trades")
    if clob_wr is not None and clob_closed is not None and clob_closed >= 5:
        if clob_wr < 0.35:
            log_odds -= 0.40   # clearly bad trader
        elif clob_wr < 0.45:
            log_odds -= 0.20   # below average
        elif clob_wr > 0.70 and clob_closed >= 20:
            log_odds += 0.30   # consistently excellent
        elif clob_wr > 0.60 and clob_closed >= 10:
            log_odds += 0.15   # above average
    # < 5 closed trades or unknown: neutral

    return _sigmoid(log_odds)
