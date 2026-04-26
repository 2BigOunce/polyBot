"""
Reconciliation logic for cluster paper trades.

Pure functions, no side effects beyond reading/writing
data/cluster_paper_trades.jsonl + data/cluster_pnl_state.jsonl.

Used by:
  - analysis/14_cluster_reconcile.py (manual CLI invocation)
  - src/clob/cluster_tracker.py (cluster_pnl_reporter coroutine)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.request
from collections import defaultdict

CLOB_API   = "https://clob.polymarket.com"
DATA_DIR   = "data"
TRADES_LOG = os.path.join(DATA_DIR, "cluster_paper_trades.jsonl")
STATE_PATH = os.path.join(DATA_DIR, "cluster_pnl_state.jsonl")


def _http_get(url: str, timeout: float = 15.0) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polyBot-reconcile/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[reconcile] GET failed {url[:60]}..: {e}")
        return None


def fetch_market(cid: str) -> dict | None:
    return _http_get(f"{CLOB_API}/markets/{cid}")


def market_winner(market: dict | None) -> str | None:
    """Outcome label of the winning token, or None if unresolved."""
    if not market or not market.get("closed"):
        return None
    for t in (market.get("tokens") or []):
        if t.get("winner"):
            return t.get("outcome", "")
    return None


def load_trades(path: str = TRADES_LOG) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_state(path: str = STATE_PATH) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    by_tx: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                tx = rec.get("tx_hash")
                if tx:
                    by_tx[tx] = rec
            except json.JSONDecodeError:
                continue
    return by_tx


def write_state(records: list[dict], path: str = STATE_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def compute_pnl(trade: dict, winner_outcome: str) -> tuple[bool, float]:
    bought   = (trade.get("outcome") or "").upper()
    winner_u = (winner_outcome or "").upper()
    px       = float(trade.get("insider_price") or 0)
    size     = float(trade.get("our_size") or 0)
    if px <= 0 or size <= 0:
        return False, 0.0
    if bought == winner_u:
        return True, size * (1.0 - px) / px
    return False, -size


def reconcile(refresh: bool = False) -> tuple[list[dict], int, int]:
    """
    Returns (out_records, n_pending, n_newly_resolved).
    Side effect: writes STATE_PATH.
    """
    trades = load_trades()
    state  = load_state()
    if not trades:
        return [], 0, 0

    market_cache: dict[str, dict | None] = {}
    new_resolved = 0
    pending      = 0
    out: list[dict] = []

    for tr in trades:
        tx = tr.get("tx_hash")
        if not tx:
            continue
        existing = state.get(tx)
        if existing and existing.get("resolved") and not refresh:
            out.append(existing)
            continue
        cid = tr.get("conditionId")
        if not cid:
            continue
        if cid not in market_cache:
            market_cache[cid] = fetch_market(cid)
            time.sleep(0.05)
        winner = market_winner(market_cache[cid])
        rec = dict(tr)
        if winner is None:
            rec.update({"resolved": False, "won": None, "pnl_usdc": None,
                        "winner_outcome": None})
            pending += 1
        else:
            won, pnl = compute_pnl(tr, winner)
            rec.update({
                "resolved": True, "won": won, "pnl_usdc": round(pnl, 4),
                "winner_outcome": winner,
                "reconciled_at_iso": dt.datetime.now(dt.timezone.utc).isoformat(),
            })
            if not (existing and existing.get("resolved")):
                new_resolved += 1
        out.append(rec)

    write_state(out)
    return out, pending, new_resolved


def format_summary(records: list[dict], pending: int, new_resolved: int) -> str:
    """Multi-line text summary suitable for stdout or Telegram."""
    resolved = [r for r in records if r.get("resolved")]
    if not resolved:
        return f"Cluster paper P&L\nNo resolved trades yet ({pending} pending)."

    won  = [r for r in resolved if r.get("won")]
    pnl_total = sum(r["pnl_usdc"] for r in resolved)
    usdc_in   = sum(float(r.get("our_size", 0) or 0) for r in resolved)
    roi = (100 * pnl_total / usdc_in) if usdc_in else 0.0

    by_w: dict[str, list[dict]] = defaultdict(list)
    for r in resolved:
        by_w[r.get("wallet", "?")].append(r)

    lines: list[str] = []
    lines.append("📊 CLUSTER PAPER P&L")
    lines.append(f"Resolved: {len(resolved)}  |  Pending: {pending}  |  New today: {new_resolved}")
    lines.append(f"Win rate: {len(won)}/{len(resolved)} = {100*len(won)/len(resolved):.0f}%")
    lines.append(f"USDC in: ${usdc_in:,.0f}   P&L: ${pnl_total:+,.2f}   ROI: {roi:+.1f}%")
    lines.append("")
    lines.append("By wallet:")
    for w, rs in sorted(by_w.items(), key=lambda kv: -sum(x["pnl_usdc"] for x in kv[1])):
        ws = sum(1 for r in rs if r["won"])
        pnl = sum(r["pnl_usdc"] for r in rs)
        lines.append(f"  {w[:10]}.. {len(rs):>3d} trades  {ws}W  ${pnl:>+8.2f}")

    # Last 24h delta
    cutoff = time.time() - 24 * 3600
    recent = [r for r in resolved
              if (r.get("trade_ts") or 0) >= cutoff
              or _iso_to_ts(r.get("reconciled_at_iso", "")) >= cutoff]
    if recent:
        recent_pnl = sum(r["pnl_usdc"] for r in recent)
        recent_w   = sum(1 for r in recent if r["won"])
        lines.append("")
        lines.append(f"Last 24h: {len(recent)} resolved, {recent_w}W, ${recent_pnl:+.2f}")

    return "\n".join(lines)


def _iso_to_ts(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
