"""Accounting: the SQLite ledger and the daily report generator.

Rejection stats are the product here as much as PnL is: the daily report
explicitly calls out when safety thresholds look too loose (>70% of
candidates passing), because a bot that rejects almost nothing isn't
filtering, it's rubber-stamping.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from bot.models import Candidate, Fill, Position, SafetyVerdict, now_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    symbol TEXT,
    source TEXT,
    discovered_at REAL,
    liquidity_usd REAL,
    volume_5m_usd REAL,
    buys_5m INTEGER,
    sells_5m INTEGER,
    price_usd REAL,
    leader_wallet TEXT
);

CREATE TABLE IF NOT EXISTS safety_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    passed INTEGER NOT NULL,
    reasons TEXT,
    checks_json TEXT,
    timestamp REAL
);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    mint TEXT NOT NULL,
    symbol TEXT,
    size_sol REAL,
    entry_price_usd REAL,
    tokens_held REAL,
    opened_at REAL,
    source TEXT,
    leader_wallet TEXT,
    status TEXT,
    closed_at REAL,
    realized_pnl_sol REAL
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT,
    mint TEXT,
    side TEXT,
    quote_price_usd REAL,
    fill_price_usd REAL,
    size_sol REAL,
    tokens REAL,
    fee_sol REAL,
    slippage_bps REAL,
    tx_sig TEXT,
    timestamp REAL,
    reason TEXT
);

-- Periodic snapshots of RpcGateway.get_call_stats(), so the RPC budget is
-- something you can audit after the fact via --daily-report, not just
-- something visible while the bot happens to be running.
CREATE TABLE IF NOT EXISTS rpc_budget_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL,
    calls_per_minute INTEGER,
    rate_limit_per_10s INTEGER,
    budget_usage_pct REAL,
    total_calls INTEGER,
    top_methods_json TEXT
);

-- Periodic snapshots of InsiderRadar.get_stats(). InsiderRadar's own state
-- (wallet scores, leaderboards) lives entirely in memory in the running
-- process -- this table is the ONLY way a one-shot CLI command (which
-- never constructs an InsiderRadar) can show indexing activity at all.
CREATE TABLE IF NOT EXISTS radar_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL,
    wallets_indexed INTEGER,
    tokens_tracked INTEGER,
    total_buy_events INTEGER,
    total_sell_events INTEGER,
    sniper_count INTEGER,
    conviction_count INTEGER,
    unfollowed_count INTEGER,
    watch_list_size INTEGER
);
"""


def _day_bounds(date_str: str) -> tuple[float, float]:
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return start.timestamp(), start.timestamp() + 86400.0


class Accounting:
    def __init__(self, db_path: str = "data/bot.db", logger: Optional[logging.Logger] = None) -> None:
        self.db_path = db_path
        self.logger = logger or logging.getLogger("memebot.accounting")
        if db_path != ":memory:":
            import os

            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def record_candidate(self, candidate: Candidate) -> None:
        self.conn.execute(
            """INSERT INTO candidates
               (mint, symbol, source, discovered_at, liquidity_usd, volume_5m_usd, buys_5m, sells_5m, price_usd, leader_wallet)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate.mint,
                candidate.symbol,
                candidate.source.value,
                candidate.discovered_at,
                candidate.liquidity_usd,
                candidate.volume_5m_usd,
                candidate.buys_5m,
                candidate.sells_5m,
                candidate.price_usd,
                candidate.leader_wallet,
            ),
        )
        self.conn.commit()

    def record_safety_verdict(self, verdict: SafetyVerdict) -> None:
        checks_json = json.dumps([asdict(c) for c in verdict.checks])
        self.conn.execute(
            "INSERT INTO safety_verdicts (mint, passed, reasons, checks_json, timestamp) VALUES (?, ?, ?, ?, ?)",
            (verdict.mint, int(verdict.passed), json.dumps(verdict.rejection_reasons), checks_json, verdict.timestamp),
        )
        self.conn.commit()

    def record_position_opened(self, position: Position) -> None:
        self.conn.execute(
            """INSERT INTO positions
               (id, mint, symbol, size_sol, entry_price_usd, tokens_held, opened_at, source, leader_wallet, status, closed_at, realized_pnl_sol)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)""",
            (
                position.id,
                position.mint,
                position.symbol,
                position.size_sol,
                position.entry_price_usd,
                position.tokens_held,
                position.opened_at,
                position.source.value,
                position.leader_wallet,
                position.status.value,
            ),
        )
        self.conn.commit()

    def record_position_closed(self, position: Position) -> None:
        self.conn.execute(
            "UPDATE positions SET status = ?, closed_at = ?, realized_pnl_sol = ? WHERE id = ?",
            (position.status.value, position.closed_at, position.realized_pnl_sol, position.id),
        )
        self.conn.commit()

    def record_fill(self, fill: Fill) -> None:
        self.conn.execute(
            """INSERT INTO fills
               (position_id, mint, side, quote_price_usd, fill_price_usd, size_sol, tokens, fee_sol, slippage_bps, tx_sig, timestamp, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fill.position_id,
                fill.mint,
                fill.side,
                fill.quote_price_usd,
                fill.fill_price_usd,
                fill.size_sol,
                fill.tokens,
                fill.fee_sol,
                fill.slippage_bps,
                fill.tx_sig,
                fill.timestamp,
                fill.reason,
            ),
        )
        self.conn.commit()

    def record_rpc_snapshot(self, stats: dict, timestamp: Optional[float] = None) -> None:
        """`stats` is RpcGateway.get_call_stats()'s output. Called
        periodically by Orchestrator (not per-RPC-call -- that would just
        move the budget problem into SQLite writes)."""
        self.conn.execute(
            """INSERT INTO rpc_budget_snapshots
               (timestamp, calls_per_minute, rate_limit_per_10s, budget_usage_pct, total_calls, top_methods_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                timestamp if timestamp is not None else now_ts(),
                stats.get("calls_per_minute", 0),
                stats.get("rate_limit_per_10s", 0),
                stats.get("budget_usage_pct", 0.0),
                stats.get("total_calls", 0),
                json.dumps(stats.get("top_methods", {})),
            ),
        )
        self.conn.commit()

    def record_radar_snapshot(self, stats: dict, timestamp: Optional[float] = None) -> None:
        """`stats` is InsiderRadar.get_stats()'s output."""
        self.conn.execute(
            """INSERT INTO radar_snapshots
               (timestamp, wallets_indexed, tokens_tracked, total_buy_events, total_sell_events,
                sniper_count, conviction_count, unfollowed_count, watch_list_size)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp if timestamp is not None else now_ts(),
                stats.get("wallets_indexed", 0),
                stats.get("tokens_tracked", 0),
                stats.get("total_buy_events", 0),
                stats.get("total_sell_events", 0),
                stats.get("sniper_count", 0),
                stats.get("conviction_count", 0),
                stats.get("unfollowed_count", 0),
                stats.get("watch_list_size", 0),
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # reads / reporting
    # ------------------------------------------------------------------

    def per_leader_pnl(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT leader_wallet, COUNT(*) as trades, SUM(realized_pnl_sol) as pnl_sol
               FROM positions WHERE leader_wallet IS NOT NULL AND status = 'closed'
               GROUP BY leader_wallet ORDER BY pnl_sol DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_radar_snapshot(self) -> Optional[dict]:
        """The single most recent InsiderRadar snapshot, regardless of what
        day it landed on -- unlike daily_report, this is "current state,"
        not "today's activity," since indexing progress spans days by
        design (see the cold-start discussion in HONESTY.md)."""
        row = self.conn.execute("SELECT * FROM radar_snapshots ORDER BY timestamp DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def daily_report(self, date_str: Optional[str] = None) -> dict:
        date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start, end = _day_bounds(date_str)

        signal_count = self.conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE discovered_at >= ? AND discovered_at < ?", (start, end)
        ).fetchone()[0]

        verdicts = self.conn.execute(
            "SELECT passed, reasons FROM safety_verdicts WHERE timestamp >= ? AND timestamp < ?", (start, end)
        ).fetchall()
        total_verdicts = len(verdicts)
        passed_count = sum(1 for v in verdicts if v["passed"])
        pass_rate = (passed_count / total_verdicts) if total_verdicts else 0.0

        reason_counter: Counter[str] = Counter()
        for v in verdicts:
            if not v["passed"]:
                for reason in json.loads(v["reasons"] or "[]"):
                    check_name = reason.split(":", 1)[0]
                    reason_counter[check_name] += 1
        top_rejection_reasons = reason_counter.most_common(5)

        closed_positions = self.conn.execute(
            "SELECT * FROM positions WHERE closed_at >= ? AND closed_at < ?", (start, end)
        ).fetchall()
        trades = len(closed_positions)
        pnl_after_fees = sum(p["realized_pnl_sol"] or 0.0 for p in closed_positions)

        fills_today = self.conn.execute(
            "SELECT * FROM fills WHERE timestamp >= ? AND timestamp < ?", (start, end)
        ).fetchall()
        total_fees = sum(f["fee_sol"] or 0.0 for f in fills_today)
        pnl_before_fees = pnl_after_fees + total_fees

        rpc_snapshots = self.conn.execute(
            "SELECT * FROM rpc_budget_snapshots WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
            (start, end),
        ).fetchall()
        if rpc_snapshots:
            avg_calls_per_minute = sum(s["calls_per_minute"] for s in rpc_snapshots) / len(rpc_snapshots)
            peak_calls_per_minute = max(s["calls_per_minute"] for s in rpc_snapshots)
            peak_budget_usage_pct = max(s["budget_usage_pct"] for s in rpc_snapshots)
            top_methods = json.loads(rpc_snapshots[-1]["top_methods_json"] or "{}")
        else:
            avg_calls_per_minute = peak_calls_per_minute = peak_budget_usage_pct = 0
            top_methods = {}
        rpc_budget = {
            "snapshots": len(rpc_snapshots),
            "avg_calls_per_minute": round(avg_calls_per_minute, 1),
            "peak_calls_per_minute": peak_calls_per_minute,
            "peak_budget_usage_pct": round(peak_budget_usage_pct, 4),
            "top_methods": top_methods,
        }

        report: dict = {
            "date": date_str,
            "signals": signal_count,
            "safety_checks": total_verdicts,
            "safety_pass_rate": round(pass_rate, 4),
            "top_rejection_reasons": top_rejection_reasons,
            "trades_closed": trades,
            "pnl_before_fees_sol": round(pnl_before_fees, 6),
            "pnl_after_fees_sol": round(pnl_after_fees, 6),
            "total_fees_sol": round(total_fees, 6),
            "per_leader_pnl": self.per_leader_pnl(),
            "rpc_budget": rpc_budget,
            "warnings": [],
        }
        if total_verdicts > 0 and pass_rate > 0.70:
            report["warnings"].append(
                f"safety pass rate is {pass_rate:.0%} (> 70%) -- thresholds are almost certainly too loose"
            )
        if rpc_snapshots and peak_budget_usage_pct >= 0.90:
            report["warnings"].append(
                f"RPC budget usage peaked at {peak_budget_usage_pct:.0%} today -- one step from 429s/an outage halt"
            )
        return report

    def render_daily_report(self, date_str: Optional[str] = None) -> str:
        r = self.daily_report(date_str)
        lines = [
            f"=== Daily Report {r['date']} ===",
            f"Signals discovered: {r['signals']}",
            f"Safety checks run: {r['safety_checks']} (pass rate {r['safety_pass_rate']:.0%})",
            "Top rejection reasons: " + (", ".join(f"{name}={n}" for name, n in r["top_rejection_reasons"]) or "none"),
            f"Trades closed: {r['trades_closed']}",
            f"PnL before fees: {r['pnl_before_fees_sol']:.4f} SOL",
            f"PnL after fees:  {r['pnl_after_fees_sol']:.4f} SOL",
            f"Total fees paid: {r['total_fees_sol']:.4f} SOL",
        ]
        if r["per_leader_pnl"]:
            lines.append("Per-leader PnL:")
            for row in r["per_leader_pnl"]:
                lines.append(f"  {row['leader_wallet']}: {row['trades']} trades, {row['pnl_sol']:.4f} SOL")

        rb = r["rpc_budget"]
        if rb["snapshots"]:
            top_methods_str = ", ".join(f"{name}={n}" for name, n in rb["top_methods"].items()) or "none"
            lines.append(
                f"RPC budget: avg {rb['avg_calls_per_minute']:.1f}/min, peak {rb['peak_calls_per_minute']}/min, "
                f"peak usage {rb['peak_budget_usage_pct']:.0%} of the 10s rate limit"
            )
            lines.append(f"RPC top methods (most recent snapshot): {top_methods_str}")
        else:
            lines.append("RPC budget: no snapshots recorded today (bot wasn't running, or just started)")

        for w in r["warnings"]:
            lines.append(f"WARNING: {w}")
        return "\n".join(lines)
