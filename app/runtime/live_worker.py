import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QObject, Signal

from app.brokers.kalshi_client import KalshiClient
from app.core.controller import Controller
from app.feeds.btc_feed import BTCFeed
from app.feeds.spot_client import SpotClient
from app.strategy.engine import should_exit
from app.strategy.fair_value import compute_fair_value


@dataclass
class PendingOrder:
    order_id: str
    client_order_id: str
    ticker: str
    side: str
    qty: int
    submitted_price: int
    submitted_ts: str
    state: str
    reason: str


@dataclass
class ActivePosition:
    ticker: str
    title: str
    side: str
    qty: int
    entry_price: int
    entry_ts: str
    order_id: str
    status: str
    exit_rule: str
    current_exit_ref: float
    unrealized_pnl: float
    close_time: str


@dataclass
class SessionTrade:
    ticker: str
    title: str
    side: str
    qty: int
    entry_price: int
    exit_price: int | None
    realized_pnl: float | None
    entry_ts: str
    exit_ts: str | None
    exit_reason: str | None
    resolved: bool


class LiveWorker(QObject):
    log_signal = Signal(str)
    error_signal = Signal(str)
    loop_status_signal = Signal(str)
    series_rows_signal = Signal(list)
    active_positions_signal = Signal(list)
    history_signal = Signal(list)
    spot_signal = Signal(float)
    selected_market_signal = Signal(str)
    cash_balance_signal = Signal(int)
    session_pnl_signal = Signal(int)

    def __init__(
        self,
        client: KalshiClient,
        controller: Controller,
        start_balance_cents: int,
        db,
        spot_client: SpotClient | None = None,
    ):
        super().__init__()
        self.client = client
        self.controller = controller
        self.db = db
        self.spot_client = spot_client or SpotClient()
        self.start_balance_cents = start_balance_cents
        self.current_balance_cents = start_balance_cents
        self._running = False

        self.pending_order: PendingOrder | None = None
        self.active_position: ActivePosition | None = None
        self.history: list[SessionTrade] = []
        self.active_trade_id: int | None = None

        self._last_spot = 0.0
        self._last_series = 0.0
        self._last_eval = 0.0
        self._last_balance = 0.0
        self._last_heartbeat = 0.0

    def start(self) -> None:
        self._running = True
        self.loop_status_signal.emit("Loop: running")
        self.log_signal.emit("Live loop started")
        self._log_event("loop_started", {})
        while self._running:
            self.run_cycle()
            time.sleep(1)
        self.loop_status_signal.emit("Loop: stopped")
        self.log_signal.emit("Live loop stopped")
        self._log_event("loop_stopped", {})

    def stop(self) -> None:
        self._running = False

    def run_cycle(self) -> None:
        now = time.monotonic()
        self.loop_status_signal.emit("Loop: polling")
        if now - self._last_heartbeat >= 10:
            self.log_signal.emit("Loop heartbeat: polling market data")
            self._last_heartbeat = now

        if now - self._last_spot >= 3:
            self._refresh_spot()
            self._last_spot = now

        rows: list[dict[str, Any]] = []
        if now - self._last_series >= 5:
            rows = self._refresh_series_quotes()
            self._last_series = now

        if now - self._last_eval >= 4:
            self._evaluate_and_trade(rows)
            self._last_eval = now

        if now - self._last_balance >= 25:
            self._refresh_balance()
            self._last_balance = now

        self.active_positions_signal.emit([asdict(self.active_position)] if self.active_position else [])
        self.history_signal.emit([asdict(t) for t in self.history])

    def _refresh_spot(self) -> None:
        try:
            spot = self.spot_client.fetch_btc_spot()
            self.controller.feed.push(spot)
            self.spot_signal.emit(spot)
        except Exception as exc:
            self.error_signal.emit(f"Spot refresh failed: {exc}")

    def _refresh_series_quotes(self) -> list[dict[str, Any]]:
        try:
            rows = self.client.get_hourly_series_quote_rows()
            if not rows:
                self.log_signal.emit("No open KXBTC1H contracts found")
                self.series_rows_signal.emit([])
                return []
            selected = self.client.resolve_btc_target_market()
            selected_ticker = selected.get("ticker") if selected else ""
            self.selected_market_signal.emit(selected_ticker or "")
            for row in rows:
                row["selected"] = "Yes" if selected_ticker and row.get("ticker") == selected_ticker else ""
            self.series_rows_signal.emit(rows)
            self.log_signal.emit(f"Series quote refresh complete: {len(rows)} contracts updated")
            self._log_event("series_refreshed", {"count": len(rows)})
            return rows
        except Exception as exc:
            self.error_signal.emit(f"Series refresh failed: {exc}")
            self._log_event("loop_error", {"reason": str(exc)})
            return []

    def _evaluate_and_trade(self, rows: list[dict[str, Any]]) -> None:
        if self.active_position or self.pending_order:
            self._monitor_pending_or_position(rows)
            return
        selected = next((r for r in rows if r.get("selected") == "Yes"), None)
        if not selected:
            return
        if not self.controller.feed.prices():
            return
        quote = {
            "yes_bid": selected.get("yes_bid") or 0,
            "yes_ask": selected.get("yes_ask") or 0,
            "no_bid": selected.get("no_bid") or 0,
            "no_ask": selected.get("no_ask") or 0,
        }
        if not quote["yes_ask"] or not quote["no_ask"]:
            return
        close_dt = datetime.fromisoformat(selected["close_time"].replace("Z", "+00:00"))
        seconds_to_expiry = int((close_dt - datetime.now(timezone.utc)).total_seconds())
        action = self.controller.evaluate(quote, max(seconds_to_expiry, 0))
        self.log_signal.emit(f"Entry decision: {action}")
        if action not in {"buy_yes", "buy_no"}:
            return

        side = "yes" if action == "buy_yes" else "no"
        price = int(quote["yes_ask"] if side == "yes" else quote["no_ask"])
        client_order_id = f"hourbtc-{int(time.time())}-{side}"
        try:
            resp = self.client.place_limit_order(selected["ticker"], side, 1, price, client_order_id)
            order_id = str(resp.get("order", {}).get("order_id") or resp.get("order_id") or client_order_id)
            self.pending_order = PendingOrder(
                order_id=order_id,
                client_order_id=client_order_id,
                ticker=selected["ticker"],
                side=action,
                qty=1,
                submitted_price=price,
                submitted_ts=datetime.now(timezone.utc).isoformat(),
                state="submitted",
                reason="strategy_entry",
            )
            self.log_signal.emit(f"Entry submitted: {selected['ticker']} {action} @ {price}")
            self._log_event("entry_submitted", asdict(self.pending_order))
        except Exception as exc:
            self.error_signal.emit(f"Entry placement failed: {exc}")
            self._log_event("loop_error", {"reason": str(exc)})

    def _monitor_pending_or_position(self, rows: list[dict[str, Any]]) -> None:
        if self.pending_order and not self.active_position:
            try:
                status = self.client.get_order_status(self.pending_order.order_id)
                raw = status.get("order", status)
                st = (raw.get("status") or "").lower()
                if st in {"filled", "executed"}:
                    row = next((r for r in rows if r.get("ticker") == self.pending_order.ticker), {})
                    self.active_position = ActivePosition(
                        ticker=self.pending_order.ticker,
                        title=row.get("title", ""),
                        side=self.pending_order.side,
                        qty=self.pending_order.qty,
                        entry_price=self.pending_order.submitted_price,
                        entry_ts=self.pending_order.submitted_ts,
                        order_id=self.pending_order.order_id,
                        status="open",
                        exit_rule="invalidation",
                        current_exit_ref=0,
                        unrealized_pnl=0,
                        close_time=row.get("close_time", ""),
                    )
                    self.pending_order = None
                    if self.db:
                        self.active_trade_id = self.db.insert_trade_open(
                            market_ticker=self.active_position.ticker,
                            side=self.active_position.side,
                            quantity=self.active_position.qty,
                            entry_price=self.active_position.entry_price,
                            entry_ts=self.active_position.entry_ts,
                            strategy_version="1h-live",
                            diagnostics_entry={"order_id": self.active_position.order_id},
                        )
                    self.log_signal.emit("Entry filled")
                    self._log_event("entry_filled", asdict(self.active_position))
                elif st in {"canceled", "cancelled", "rejected"}:
                    self.log_signal.emit(f"Entry not filled: {st}")
                    self.pending_order = None
            except Exception as exc:
                self.error_signal.emit(f"Order status refresh failed: {exc}")
            return

        if not self.active_position:
            return
        row = next((r for r in rows if r.get("ticker") == self.active_position.ticker), None)
        if row:
            exit_ref = row.get("yes_bid") if self.active_position.side == "buy_yes" else row.get("no_bid")
            if exit_ref is None:
                exit_ref = self.active_position.entry_price
            self.active_position.current_exit_ref = float(exit_ref)
            direction = 1 if self.active_position.side == "buy_yes" else -1
            self.active_position.unrealized_pnl = (float(exit_ref) - self.active_position.entry_price) * direction * self.active_position.qty

            try:
                fair = compute_fair_value(
                    self.controller.feed.prices()[-1],
                    row.get("strike") or self.controller.state.strike or self.controller.feed.prices()[-1],
                    row.get("yes_ask") or self.active_position.entry_price,
                    row.get("no_ask") or self.active_position.entry_price,
                    self.controller.feed.prices(),
                    60,
                )
                regime = fair.reasons[0].split("=")[-1]
                should, reason = should_exit(self.active_position.side, regime, fair, True, abs(self.active_position.unrealized_pnl))
                if should:
                    self._close_active_position(reason)
            except Exception as exc:
                self.error_signal.emit(f"Position monitoring error: {exc}")

    def _close_active_position(self, reason: str) -> None:
        if not self.active_position:
            return
        now = datetime.now(timezone.utc).isoformat()
        trade = SessionTrade(
            ticker=self.active_position.ticker,
            title=self.active_position.title,
            side=self.active_position.side,
            qty=self.active_position.qty,
            entry_price=self.active_position.entry_price,
            exit_price=int(self.active_position.current_exit_ref),
            realized_pnl=float(self.active_position.unrealized_pnl),
            entry_ts=self.active_position.entry_ts,
            exit_ts=now,
            exit_reason=reason,
            resolved=reason == "hold_to_resolution",
        )
        self.history.append(trade)
        if self.db and self.active_trade_id is not None:
            self.db.close_trade(
                trade_id=self.active_trade_id,
                exit_price=trade.exit_price or 0,
                exit_ts=trade.exit_ts or now,
                realized_pnl=trade.realized_pnl or 0,
                exit_reason=trade.exit_reason or "unknown",
                reached_resolution=trade.resolved,
                diagnostics_exit={"status": "closed"},
            )
            self.active_trade_id = None
        self.log_signal.emit(f"Position closed: {reason}")
        self._log_event("position_resolved", asdict(trade))
        self.active_position = None

    def _refresh_balance(self) -> None:
        try:
            summary = self.client.get_account_summary()
            balance = summary.get("balance")
            balance_cents = int(float(balance)) if balance is not None else self.current_balance_cents
            self.current_balance_cents = balance_cents
            pnl = balance_cents - self.start_balance_cents
            self.cash_balance_signal.emit(balance_cents)
            self.session_pnl_signal.emit(pnl)
            sign = "+" if pnl >= 0 else "-"
            self.log_signal.emit(
                f"Balance recheck complete. Cash: ${balance_cents/100:.2f} | Session PnL: {sign}${abs(pnl)/100:.2f}"
            )
            self._log_event("balance_recheck", {"balance_cents": balance_cents, "session_pnl_cents": pnl})
        except Exception as exc:
            self.error_signal.emit(f"Balance refresh failed: {exc}")

    def _log_event(self, event_type: str, details: dict[str, Any]) -> None:
        if not self.db:
            return
        try:
            self.db.log_event(datetime.now(timezone.utc).isoformat(), event_type, details)
        except Exception:
            pass
