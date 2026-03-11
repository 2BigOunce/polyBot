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


def get_deposit_amount(wallet: str) -> float:
    """Return the largest deposit amount recorded for a wallet (used for sizing)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(amount_usdc) FROM deposits WHERE wallet_address = ?",
            (wallet,),
        ).fetchone()
    return float(row[0]) if row[0] else 0.0


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
