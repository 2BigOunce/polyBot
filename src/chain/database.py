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
                detected_at     TEXT NOT NULL
            )
        """)


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
