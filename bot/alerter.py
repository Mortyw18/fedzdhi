"""Alerter: Telegram notifications + /status and /stop remote control.

Safe by default: with no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured,
every notify_* call just logs instead of raising, so running without
Telegram configured (e.g. in CI, or a first paper-mode run) is never an
error -- it's just quieter.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional, Union

import requests

from bot.models import ExitReason, Fill, Position

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

StatusCallback = Callable[[], str]
StopCallback = Callable[[], Union[None, Awaitable[None]]]


class Alerter:
    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        session: Optional[requests.Session] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = session or requests.Session()
        self.logger = logger or logging.getLogger("memebot.alerter")
        self._update_offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def notify(self, text: str) -> None:
        self.logger.info("alert: %s", text)
        if not self.enabled:
            return
        try:
            url = TELEGRAM_API.format(token=self.bot_token, method="sendMessage")
            self.session.post(url, json={"chat_id": self.chat_id, "text": text}, timeout=10.0)
        except requests.RequestException as exc:
            self.logger.warning("telegram send failed: %s", exc)

    def notify_entry(self, position: Position) -> None:
        leader_note = f" (copying {position.leader_wallet})" if position.leader_wallet else ""
        self.notify(
            f"ENTRY {position.symbol} ({position.mint[:8]}...) size={position.size_sol:.4f} SOL"
            f"{leader_note} [{position.source.value}]"
        )

    def notify_exit(self, position: Position, fill: Fill, reason: ExitReason, pnl_sol: float) -> None:
        self.notify(
            f"EXIT {position.symbol} reason={reason.value} sold={fill.size_sol:.4f} SOL "
            f"pnl_this_fill={pnl_sol:+.4f} SOL slippage={fill.slippage_bps:.0f}bps"
        )

    def notify_kill_switch(self, reason: str) -> None:
        self.notify(f"KILL SWITCH TRIPPED: {reason}. Manual reset required (--reset-kill-switch).")

    def notify_rejection_digest(self, report: dict) -> None:
        top = ", ".join(f"{name}={n}" for name, n in report.get("top_rejection_reasons", []))
        self.notify(
            f"Daily digest {report['date']}: {report['signals']} signals, "
            f"{report['safety_pass_rate']:.0%} pass rate, top rejections: {top or 'none'}"
        )
        for w in report.get("warnings", []):
            self.notify(f"WARNING: {w}")

    def notify_leader_digest(self, per_leader: list[dict]) -> None:
        if not per_leader:
            self.notify("Weekly leader digest: no closed leader-attributed trades yet.")
            return
        lines = [f"{row['leader_wallet'][:8]}...: {row['trades']} trades, {row['pnl_sol']:+.4f} SOL" for row in per_leader]
        self.notify("Weekly leader PnL digest:\n" + "\n".join(lines))

    # ------------------------------------------------------------------
    # /status and /stop
    # ------------------------------------------------------------------

    def _get_updates(self) -> list[dict]:
        url = TELEGRAM_API.format(token=self.bot_token, method="getUpdates")
        resp = self.session.get(url, params={"offset": self._update_offset + 1, "timeout": 20}, timeout=25.0)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", [])

    def poll_commands_once(
        self,
        on_status: StatusCallback,
        on_stop: StopCallback,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        """Runs inside a worker thread (see run_command_loop's run_in_executor),
        so there is no running event loop in this thread to schedule onto --
        `loop` is the real loop, captured by the caller while it was still
        running, and coroutines are handed to it via run_coroutine_threadsafe.
        """
        if not self.enabled:
            return
        try:
            updates = self._get_updates()
        except requests.RequestException as exc:
            self.logger.warning("telegram getUpdates failed: %s", exc)
            return
        for update in updates:
            self._update_offset = max(self._update_offset, update.get("update_id", self._update_offset))
            message = update.get("message") or {}
            text = (message.get("text") or "").strip().lower()
            if text == "/status":
                self.notify(on_status())
            elif text == "/stop":
                result = on_stop()
                if asyncio.iscoroutine(result):
                    if loop is not None:
                        asyncio.run_coroutine_threadsafe(result, loop)
                    else:
                        result.close()  # nowhere to schedule it -- avoid an "unawaited coroutine" warning
                self.notify("Stop requested. Shutting down after current cycle.")

    async def run_command_loop(
        self, on_status: StatusCallback, on_stop: StopCallback, stop_event: Optional[asyncio.Event] = None
    ) -> None:
        if not self.enabled:
            return
        loop = asyncio.get_running_loop()
        while stop_event is None or not stop_event.is_set():
            await loop.run_in_executor(None, self.poll_commands_once, on_status, on_stop, loop)
