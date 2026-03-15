import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "bot.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # rows behave like dicts
    return conn


def init_db():
    """Create tables if they don't exist yet."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                tx_hash         TEXT PRIMARY KEY,
                wallet_address  TEXT NOT NULL,
                amount_usdc     REAL NOT NULL,
                wallet_age_days REAL NOT NULL,
                alert_level     TEXT NOT NULL,
                detected_at     TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'active'
            )
        """)
        # Add status column to existing databases that predate this column
        try:
            conn.execute("ALTER TABLE deposits ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        except Exception:
            pass  # column already exists

        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id    TEXT PRIMARY KEY,
                wallet      TEXT NOT NULL,
                market      TEXT NOT NULL,
                side        TEXT,
                size        REAL,
                price       REAL,
                outcome     TEXT,
                match_time  TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                followed_wallet TEXT NOT NULL,
                market          TEXT NOT NULL,
                outcome         TEXT NOT NULL,
                entry_price     REAL NOT NULL,
                simulated_size  REAL NOT NULL,
                entered_at      TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'open',
                pnl             REAL,
                closed_at       TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS real_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                followed_wallet TEXT NOT NULL,
                market          TEXT NOT NULL,
                outcome         TEXT NOT NULL,
                token_id        TEXT NOT NULL,
                insider_price   REAL NOT NULL,
                fill_price      REAL,
                size_usdc       REAL NOT NULL,
                order_id        TEXT,
                entered_at      TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'open',
                pnl             REAL,
                closed_at       TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS super_alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet          TEXT NOT NULL,
                market          TEXT NOT NULL,
                outcome         TEXT NOT NULL,
                price           REAL NOT NULL,
                trade_size      REAL NOT NULL,
                max_deposit     REAL NOT NULL,
                buys_on_market  INTEGER NOT NULL,
                detected_at     TEXT NOT NULL
            )
        """)


def save_trade(
    trade_id: str,
    wallet: str,
    market: str,
    side: str,
    size: float,
    price: float,
    outcome: str,
    match_time: str,
):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO trades
                (trade_id, wallet, market, side, size, price, outcome, match_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (trade_id, wallet.lower(), market, side, size, price, outcome, match_time),
        )


def has_traded(wallet: str) -> bool:
    """Return True if any trade has been recorded for this wallet."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM trades WHERE wallet = ? LIMIT 1", (wallet.lower(),)
        ).fetchone()
    return row is not None


def get_wallet_market_stats(wallet: str, market: str) -> dict:
    """
    Return stats used for super-alert detection:
      - distinct_markets : number of distinct markets this wallet has BUYed on
      - buys_on_market   : number of BUY trades on the given market
      - max_deposit      : largest single deposit amount for this wallet
    """
    with get_connection() as conn:
        distinct = conn.execute(
            "SELECT COUNT(DISTINCT market) FROM trades WHERE wallet = ? AND side = 'BUY'",
            (wallet.lower(),),
        ).fetchone()[0]
        buys = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE wallet = ? AND market = ? AND side = 'BUY'",
            (wallet.lower(), market),
        ).fetchone()[0]
        row = conn.execute(
            "SELECT COALESCE(MAX(amount_usdc), 0) FROM deposits WHERE wallet_address = ?",
            (wallet.lower(),),
        ).fetchone()
        max_deposit = row[0] if row else 0
    return {"distinct_markets": distinct, "buys_on_market": buys, "max_deposit": max_deposit}


def get_wallets_latest_buy(wallets: list[str]) -> dict[str, dict]:
    """
    For each wallet, return its most recent BUY trade (market, outcome, size, price).
    Returns a dict keyed by wallet address (lowercase).
    """
    if not wallets:
        return {}
    placeholders = ",".join("?" * len(wallets))
    lower = [w.lower() for w in wallets]
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT wallet, market, outcome, size, price
            FROM trades
            WHERE wallet IN ({placeholders}) AND side = 'BUY'
            AND match_time = (
                SELECT MAX(t2.match_time) FROM trades t2
                WHERE t2.wallet = trades.wallet AND t2.side = 'BUY'
            )
            """,
            lower,
        ).fetchall()
    return {row["wallet"]: {"market": row["market"], "outcome": row["outcome"],
                            "size": row["size"], "price": row["price"]} for row in rows}


def get_wallet_trade_ids(wallet: str) -> set[str]:
    """Return all trade_ids already stored for a wallet (used to bootstrap seen_tx on startup)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT trade_id FROM trades WHERE wallet = ?", (wallet.lower(),)
        ).fetchall()
    return {row["trade_id"] for row in rows}


def mark_wallet_status(wallet: str, status: str) -> None:
    """Update the status of all deposit rows for a wallet."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE deposits SET status = ? WHERE wallet_address = ?",
            (status, wallet),
        )


def get_active_wallets_flagged_before(cutoff_iso: str) -> list[str]:
    """Return distinct wallet addresses that are still 'active' and were flagged before cutoff_iso."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT wallet_address FROM deposits
            WHERE status = 'active' AND detected_at < ?
            """,
            (cutoff_iso,),
        ).fetchall()
    return [row["wallet_address"] for row in rows]


def save_paper_trade(
    followed_wallet: str,
    market: str,
    outcome: str,
    entry_price: float,
    simulated_size: float,
) -> int:
    """Insert a new open paper trade. Returns the row id."""
    entered_at = datetime.now(tz=timezone.utc).isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO paper_trades
                (followed_wallet, market, outcome, entry_price, simulated_size, entered_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (followed_wallet.lower(), market, outcome, entry_price, simulated_size, entered_at),
        )
        return cur.lastrowid


def get_open_paper_trade(wallet: str, market: str, outcome: str) -> sqlite3.Row | None:
    """Return the open paper trade row for this wallet+market+outcome, or None."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM paper_trades WHERE followed_wallet = ? AND market = ? AND outcome = ? AND status = 'open' LIMIT 1",
            (wallet.lower(), market, outcome),
        ).fetchone()


def has_open_paper_trade(wallet: str, market: str, outcome: str) -> bool:
    """Return True if there is already an open paper trade for this wallet+market+outcome."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM paper_trades WHERE followed_wallet = ? AND market = ? AND outcome = ? AND status = 'open' LIMIT 1",
            (wallet.lower(), market, outcome),
        ).fetchone()
    return row is not None


def get_open_paper_trades() -> list[sqlite3.Row]:
    """Return all paper trades that are still open."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open'"
        ).fetchall()


def close_paper_trade(trade_id: int, status: str, pnl: float) -> None:
    """Mark a paper trade as won or lost and record the P&L."""
    closed_at = datetime.now(tz=timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE paper_trades SET status = ?, pnl = ?, closed_at = ? WHERE id = ?",
            (status, pnl, closed_at, trade_id),
        )


def get_market_exposure(market: str) -> float:
    """Return total simulated_size of all open paper trades on a given market."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(simulated_size), 0) FROM paper_trades WHERE market = ? AND status = 'open'",
            (market,),
        ).fetchone()
    return float(row[0])


def get_pnl_summary() -> dict:
    """Return aggregated P&L stats for the /pnl Telegram command."""
    with get_connection() as conn:
        won  = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status = 'won'").fetchone()
        lost = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status = 'lost'").fetchone()
        open_ = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status = 'open'").fetchone()
        top_wallet = conn.execute(
            "SELECT followed_wallet, COALESCE(SUM(pnl), 0) as total FROM paper_trades"
            " WHERE status IN ('won','lost') GROUP BY followed_wallet ORDER BY total DESC LIMIT 1"
        ).fetchone()
        best_trade = conn.execute(
            "SELECT outcome, entry_price, pnl FROM paper_trades WHERE status = 'won' ORDER BY pnl DESC LIMIT 1"
        ).fetchone()
    total = won[1] + lost[1]
    total_closed = won[0] + lost[0]
    return {
        "won_count":  won[0],
        "won_pnl":    won[1],
        "lost_count": lost[0],
        "lost_pnl":   lost[1],
        "net_pnl":    total,
        "win_rate":   won[0] / total_closed * 100 if total_closed else 0.0,
        "open_count": open_[0],
        "top_wallet": dict(top_wallet) if top_wallet else None,
        "best_trade": dict(best_trade) if best_trade else None,
    }


def get_deposit_amount(wallet: str) -> float:
    """Return the largest deposit amount recorded for a wallet (used for sizing)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(amount_usdc) FROM deposits WHERE wallet_address = ?",
            (wallet,),
        ).fetchone()
    return float(row[0]) if row[0] else 0.0


def get_real_pnl_summary() -> dict:
    """Return aggregated P&L stats for real trades (/realpnl Telegram command)."""
    with get_connection() as conn:
        won   = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM real_trades WHERE status = 'won'").fetchone()
        lost  = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM real_trades WHERE status = 'lost'").fetchone()
        open_ = conn.execute("SELECT COUNT(*), COALESCE(SUM(size_usdc), 0) FROM real_trades WHERE status = 'open'").fetchone()
        best  = conn.execute(
            "SELECT outcome, insider_price, pnl FROM real_trades WHERE status = 'won' ORDER BY pnl DESC LIMIT 1"
        ).fetchone()
    total_closed = won[0] + lost[0]
    return {
        "won_count":  won[0],  "won_pnl":  won[1],
        "lost_count": lost[0], "lost_pnl": lost[1],
        "net_pnl":    won[1] + lost[1],
        "win_rate":   won[0] / total_closed * 100 if total_closed else 0.0,
        "open_count": open_[0],
        "open_exposure": open_[1],
        "best_trade": dict(best) if best else None,
    }


def save_real_trade(
    followed_wallet: str,
    market: str,
    outcome: str,
    token_id: str,
    insider_price: float,
    fill_price: float,
    size_usdc: float,
    order_id: str,
) -> int:
    entered_at = datetime.now(tz=timezone.utc).isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO real_trades
                (followed_wallet, market, outcome, token_id, insider_price, fill_price, size_usdc, order_id, entered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (followed_wallet.lower(), market, outcome, token_id, insider_price, fill_price, size_usdc, order_id, entered_at),
        )
        return cur.lastrowid


def get_market_real_exposure(market: str, outcome: str) -> float:
    """Return total USDC currently open on a given market/outcome across all wallets."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(size_usdc), 0) FROM real_trades WHERE market = ? AND outcome = ? AND status = 'open'",
            (market, outcome),
        ).fetchone()
    return float(row[0])


def has_opposing_real_trade(market: str, outcome: str) -> bool:
    """Return True if we have an open position on the same market but a different outcome."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM real_trades WHERE market = ? AND outcome != ? AND status = 'open' LIMIT 1",
            (market, outcome),
        ).fetchone()
    return row is not None


def get_open_real_trade(wallet: str, market: str, outcome: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM real_trades WHERE followed_wallet = ? AND market = ? AND outcome = ? AND status = 'open' LIMIT 1",
            (wallet.lower(), market, outcome),
        ).fetchone()


def has_open_real_trade(wallet: str, market: str, outcome: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM real_trades WHERE followed_wallet = ? AND market = ? AND outcome = ? AND status = 'open' LIMIT 1",
            (wallet.lower(), market, outcome),
        ).fetchone()
    return row is not None


def get_open_real_trades() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM real_trades WHERE status = 'open'"
        ).fetchall()


def close_real_trade(trade_id: int, status: str, pnl: float) -> None:
    closed_at = datetime.now(tz=timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE real_trades SET status = ?, pnl = ?, closed_at = ? WHERE id = ?",
            (status, pnl, closed_at, trade_id),
        )


def save_super_alert(
    wallet: str,
    market: str,
    outcome: str,
    price: float,
    trade_size: float,
    max_deposit: float,
    buys_on_market: int,
) -> None:
    detected_at = datetime.now(tz=timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO super_alerts
                (wallet, market, outcome, price, trade_size, max_deposit, buys_on_market, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (wallet.lower(), market, outcome, price, trade_size, max_deposit, buys_on_market, detected_at),
        )


def get_recent_super_alerts(limit: int = 5) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM super_alerts ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def save_deposit(
    tx_hash: str,
    wallet_address: str,
    amount_usdc: float,
    wallet_age_days: float,
    alert_level: str,
):
    detected_at = datetime.now(tz=timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO deposits
                (tx_hash, wallet_address, amount_usdc, wallet_age_days, alert_level, detected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tx_hash, wallet_address, amount_usdc, wallet_age_days, alert_level, detected_at),
        )
