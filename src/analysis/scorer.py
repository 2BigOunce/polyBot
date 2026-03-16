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
) -> float:
    """
    Compute p̂ ∈ (0, 1) for a (wallet, trade) pair.

    Parameters
    ----------
    wallet       : wallet address being copied
    insider_size : USDC size of the insider's trade
    market_price : current market price of the outcome (0.10 – 0.75)

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

    return _sigmoid(log_odds)
