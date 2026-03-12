from src.chain.database import get_connection

# Shared in-memory set — populated at startup from DB, updated live by deposit_monitor
watched: set[str] = set()


def load_from_db():
    """Load previously flagged wallets from the database into the watched set."""
    with get_connection() as conn:
        rows = conn.execute("SELECT wallet_address FROM deposits WHERE status NOT IN ('removed', 'unreliable')").fetchall()
    for row in rows:
        watched.add(row["wallet_address"])
    print(f"[watched] Loaded {len(watched)} wallets from database")


def add(wallet_address: str):
    """Add a newly flagged wallet to the watched set."""
    watched.add(wallet_address)
    print(f"[watched] Now tracking {wallet_address} ({len(watched)} total)")
