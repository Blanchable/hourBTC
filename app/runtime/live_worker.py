import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QObject, Signal

from app.brokers.kalshi_client import BTC_1H_SERIES_TICKER, KalshiClient
from app.config.defaults import settings
from app.core.controller import Controller
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
    purpose: str
    intended_reason: str
    filled_qty: int = 0
    remaining_qty: int = 0


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
    trade_id: int | None = None


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
        self.cfg = settings.global_settings
        self.start_balance_cents = start_balance_cents
        self.current_balance_cents = start_balance_cents
        self._running = False

        self.pending_entry_order: PendingOrder | None = None
        self.pending_exit_order: PendingOrder | None = None
        self.active_position: ActivePosition | None = None
        self.history: list[SessionTrade] = []

        self.completed_trades = 0
        self.consecutive_losses = 0
        self.order_rejections = 0
        self.api_errors = 0
        self.consecutive_loop_errors = 0
        self.new_entries_disabled = False

        self._last_spot = 0.0
        self._last_series = 0.0
        self._last_eval = 0.0
        self._last_balance = 0.0
        self._last_heartbeat = 0.0

    def _new_client_order_id(self, prefix: str, side: str) -> str:
        return f"{prefix}-{side}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    def start(self) -> None:
        self._running = True
        self.loop_status_signal.emit("Loop: running")
        self.log_signal.emit("Live loop started")
        self._log_event("loop_started", {})
        validation = self.client.validate_hourly_series()
        if validation.get("ok"):
            self.log_signal.emit(f"Hourly series validated: {validation.get('ticker')}")
        else:
            self.error_signal.emit(
                f"Hourly series validation failed for {validation.get('ticker')}: {validation.get('error', 'unknown error')}"
            )
        start_ts = time.monotonic()
        while self._running:
            try:
                self.run_cycle()
                self.consecutive_loop_errors = 0
            except Exception as exc:
                self.consecutive_loop_errors += 1
                self.api_errors += 1
                self.error_signal.emit(f"Loop error: {exc}")
                self._log_event("loop_error", {"reason": str(exc)})
                if self.consecutive_loop_errors >= self.cfg.max_consecutive_loop_errors:
                    self.new_entries_disabled = True
                    self.error_signal.emit("Max consecutive loop errors reached. New entries disabled.")
            if (time.monotonic() - start_ts) > (self.cfg.session_runtime_limit_minutes * 60):
                self.log_signal.emit("Session runtime limit reached. Stopping loop.")
                self._running = False
                break
            time.sleep(1)
        self.loop_status_signal.emit("Loop: stopped")
        self.log_signal.emit("Live loop stopped")
        self._log_event("loop_stopped", {})

    def stop(self) -> None:
        self.log_signal.emit("Shutdown requested. Stopping live loop...")
        self._running = False

    def shutdown_cleanup(self) -> None:
        if self.pending_entry_order:
            try:
                self.client.cancel_order(self.pending_entry_order.order_id)
                self.log_signal.emit(f"Canceled resting entry order during shutdown: {self.pending_entry_order.order_id}")
            except Exception as exc:
                self.error_signal.emit(f"Failed to cancel resting entry on shutdown: {exc}")
        self.pending_entry_order = None

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

        self._monitor_pending_entry(rows)
        self._monitor_pending_exit(rows)

        if now - self._last_eval >= 4 and not self.pending_exit_order:
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
            self.api_errors += 1

    def _refresh_series_quotes(self) -> list[dict[str, Any]]:
        try:
            rows = self.client.get_hourly_series_quote_rows()
            if not rows:
                self.log_signal.emit(f"No open {BTC_1H_SERIES_TICKER} contracts found")
                diag = getattr(self.client, "last_series_diagnostics", {}) or {}
                self.log_signal.emit(f"{BTC_1H_SERIES_TICKER} open query returned {diag.get('open_count', 0)} rows")
                self.log_signal.emit(
                    f"{BTC_1H_SERIES_TICKER} no-status query returned {diag.get('fallback_count', 0)} rows, {diag.get('tradable_count', 0)} currently tradable"
                )
                self.series_rows_signal.emit([])
                return []
            selected = self.client.resolve_btc_target_market()
            selected_ticker = selected.get("ticker") if selected else ""
            self.selected_market_signal.emit(selected_ticker or "")
            for row in rows:
                row["selected"] = "Yes" if selected_ticker and row.get("ticker") == selected_ticker else ""
            self.series_rows_signal.emit(rows)
            self.log_signal.emit(f"Series quote refresh complete: {len(rows)} {BTC_1H_SERIES_TICKER} contracts updated")
            if selected_ticker:
                self.log_signal.emit(f"Selected target market: {selected_ticker}")
            self._log_event("series_refreshed", {"count": len(rows)})
            return rows
        except Exception as exc:
            self.error_signal.emit(f"Series refresh failed: {exc}")
            self.api_errors += 1
            self._log_event("loop_error", {"reason": str(exc)})
            return []

    def _entry_guardrail_reason(self, price: int, qty: int) -> str | None:
        session_pnl = self.current_balance_cents - self.start_balance_cents
        if self.new_entries_disabled:
            return "new_entries_disabled"
        if self.active_position or self.pending_entry_order or self.pending_exit_order:
            return "position_or_order_already_open"
        if self.completed_trades >= self.cfg.max_trades_per_session:
            return "max_trades_per_session_reached"
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return "max_consecutive_losses_reached"
        if session_pnl <= -self.cfg.session_stop_loss_cents:
            self.new_entries_disabled = self.cfg.disable_new_entries_after_stop_hit
            self._log_event("risk_stop_hit", {"type": "session_stop_loss", "session_pnl_cents": session_pnl})
            return "session_stop_loss_hit"
        if session_pnl >= self.cfg.session_take_profit_cents:
            self.new_entries_disabled = self.cfg.disable_new_entries_after_stop_hit
            self._log_event("risk_stop_hit", {"type": "session_take_profit", "session_pnl_cents": session_pnl})
            return "session_take_profit_hit"
        notional = price * qty
        if qty > self.cfg.max_position_contracts:
            return "max_position_contracts_exceeded"
        if notional > self.cfg.max_position_notional_cents:
            return "max_position_notional_exceeded"
        if self.active_position:
            total_notional = notional + (self.active_position.entry_price * self.active_position.qty)
            if total_notional > self.cfg.max_total_open_notional_cents:
                return "max_total_open_notional_exceeded"
        if self.order_rejections >= self.cfg.max_order_rejections_per_session:
            return "max_order_rejections_reached"
        if self.api_errors >= self.cfg.max_api_errors_per_session:
            return "max_api_errors_reached"
        return None

    def _evaluate_and_trade(self, rows: list[dict[str, Any]]) -> None:
        if self.active_position:
            self._monitor_active_position(rows)
            return
        selected = next((r for r in rows if r.get("selected") == "Yes"), None)
        if not selected:
            return
        if self.controller.feed.is_stale(self.cfg.quote_stale_stop_seconds):
            self.log_signal.emit("Quote/feed stale. Skipping entry.")
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
            if self.active_position:
                self._monitor_active_position(rows)
            return

        side = "yes" if action == "buy_yes" else "no"
        price = int(quote["yes_ask"] if side == "yes" else quote["no_ask"])
        qty = 1
        blocked = self._entry_guardrail_reason(price, qty)
        if blocked:
            self.log_signal.emit(f"Session guardrail hit. New entry blocked: {blocked}")
            self._log_event("session_guardrail_hit", {"reason": blocked})
            return

        client_order_id = self._new_client_order_id("hourbtc", side)
        self.log_signal.emit(
            f"Submitting entry order: ticker={selected['ticker']} side={side} action=buy qty={qty} price={price} post_only={self.cfg.entry_post_only} tif={self.cfg.entry_time_in_force}"
        )
        try:
            resp = self.client.place_entry_order(
                ticker=selected["ticker"],
                side=side,
                count=qty,
                limit_price=price,
                client_order_id=client_order_id,
                post_only=self.cfg.entry_post_only,
                time_in_force=self.cfg.entry_time_in_force,
                expiration_ts=int(time.time()) + self.cfg.entry_order_timeout_seconds,
                cancel_order_on_pause=self.cfg.cancel_order_on_pause,
            )
            order = resp.get("order", resp)
            order_id = str(order.get("order_id") or resp.get("order_id") or client_order_id)
            status = (order.get("status") or "pending").lower()
            self.pending_entry_order = PendingOrder(
                order_id=order_id,
                client_order_id=client_order_id,
                ticker=selected["ticker"],
                side=side,
                qty=qty,
                submitted_price=price,
                submitted_ts=datetime.now(timezone.utc).isoformat(),
                state=status,
                reason="strategy_entry",
                purpose="entry",
                intended_reason="qualified_entry",
                filled_qty=int(order.get("fill_count") or 0),
                remaining_qty=int(order.get("remaining_count") or qty),
            )
            self.log_signal.emit(f"Entry order accepted: order_id={order_id} status={status}")
            self._log_event("entry_submitted", asdict(self.pending_entry_order))
            if status == "resting":
                self._log_event("entry_resting", asdict(self.pending_entry_order))
        except Exception as exc:
            self.order_rejections += 1
            self.api_errors += 1
            self.error_signal.emit(f"Entry order rejected: {exc}")
            self._log_event("entry_rejected", {"reason": str(exc), "ticker": selected["ticker"]})

    def _monitor_pending_entry(self, rows: list[dict[str, Any]]) -> None:
        if not self.pending_entry_order:
            return
        order = self.pending_entry_order
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(order.submitted_ts)).total_seconds()
        try:
            status_payload = self.client.get_order_status(order.order_id)
        except Exception as exc:
            self.api_errors += 1
            self.error_signal.emit(f"Order status refresh failed: {exc}")
            return

        raw = status_payload.get("order", status_payload)
        status = str(raw.get("status") or "pending").lower()
        fill_count = int(raw.get("fill_count") or 0)
        remaining = int(raw.get("remaining_count") or max(0, order.qty - fill_count))

        if status in {"executed", "filled"} or fill_count > 0:
            filled_qty = fill_count if fill_count > 0 else order.qty
            self._create_or_update_active_position(order, rows, filled_qty)
            if remaining > 0:
                self.log_signal.emit(f"Entry partially filled: {filled_qty} contracts; remainder canceled")
                self._log_event("entry_partially_filled", {"order_id": order.order_id, "filled_qty": filled_qty, "remaining": remaining})
                try:
                    self.client.cancel_order(order.order_id)
                except Exception as exc:
                    self.error_signal.emit(f"Partial fill remainder cancel failed: {exc}")
            self.pending_entry_order = None
            self.log_signal.emit("Entry filled")
            self._log_event("entry_filled", {"order_id": order.order_id, "filled_qty": filled_qty})
            return

        if status in {"rejected"}:
            self.order_rejections += 1
            self.pending_entry_order = None
            self.error_signal.emit(f"Entry order rejected: order_id={order.order_id}")
            self._log_event("entry_rejected", {"order_id": order.order_id})
            return

        if status in {"canceled", "cancelled"}:
            self.pending_entry_order = None
            self.log_signal.emit(f"Entry canceled: order_id={order.order_id}")
            self._log_event("entry_canceled", {"order_id": order.order_id})
            return

        if status in {"pending", "resting"} and elapsed >= self.cfg.entry_order_timeout_seconds:
            self.log_signal.emit(f"Entry order timed out after {self.cfg.entry_order_timeout_seconds}s, canceling order_id={order.order_id}")
            self._log_event("entry_timed_out", {"order_id": order.order_id})
            try:
                self.client.cancel_order(order.order_id)
                self._log_event("entry_canceled", {"order_id": order.order_id, "reason": "timeout"})
            except Exception as exc:
                self.error_signal.emit(f"Failed to cancel timed-out entry order: {exc}")
            self.pending_entry_order = None

    def _create_or_update_active_position(self, order: PendingOrder, rows: list[dict[str, Any]], filled_qty: int) -> None:
        row = next((r for r in rows if r.get("ticker") == order.ticker), {})
        if self.active_position is None:
            trade_id = None
            if self.db:
                trade_id = self.db.insert_trade_open(
                    market_ticker=order.ticker,
                    side=f"buy_{order.side}",
                    quantity=filled_qty,
                    entry_price=order.submitted_price,
                    entry_ts=order.submitted_ts,
                    strategy_version="1h-live",
                    diagnostics_entry={"order_id": order.order_id, "client_order_id": order.client_order_id},
                )
            self.active_position = ActivePosition(
                ticker=order.ticker,
                title=row.get("title", ""),
                side=f"buy_{order.side}",
                qty=filled_qty,
                entry_price=order.submitted_price,
                entry_ts=order.submitted_ts,
                order_id=order.order_id,
                status="open",
                exit_rule="invalidation",
                current_exit_ref=float(order.submitted_price),
                unrealized_pnl=0.0,
                close_time=row.get("close_time", ""),
                trade_id=trade_id,
            )

    def _monitor_active_position(self, rows: list[dict[str, Any]]) -> None:
        if not self.active_position or self.pending_exit_order:
            return
        row = next((r for r in rows if r.get("ticker") == self.active_position.ticker), None)
        if not row:
            return

        side_yes = self.active_position.side == "buy_yes"
        exit_ref = row.get("yes_bid") if side_yes else row.get("no_bid")
        if exit_ref is None:
            exit_ref = self.active_position.entry_price
        self.active_position.current_exit_ref = float(exit_ref)
        direction = 1 if side_yes else -1
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
                self._submit_exit_for_active_position(reason, row)
        except Exception as exc:
            self.api_errors += 1
            self.error_signal.emit(f"Position monitoring error: {exc}")

    def _submit_exit_for_active_position(self, reason: str, row: dict | None) -> None:
        if not self.active_position or self.pending_exit_order:
            return
        side = "yes" if self.active_position.side == "buy_yes" else "no"
        bid = row.get("yes_bid") if side == "yes" else row.get("no_bid") if row else None
        if bid is None:
            self.error_signal.emit("Exit blocked: missing bid reference for offsetting sell")
            return
        tif = self.cfg.emergency_exit_time_in_force if reason in {"risk_fail_safe", "regime_flip", "fair_value_collapse"} else self.cfg.exit_time_in_force
        client_order_id = self._new_client_order_id("hourbtc-exit", side)
        self.log_signal.emit(
            f"Submitting exit order: ticker={self.active_position.ticker} side={side} action=sell qty={self.active_position.qty} price={int(bid)} reason={reason}"
        )
        try:
            resp = self.client.place_exit_order(
                ticker=self.active_position.ticker,
                side=side,
                count=self.active_position.qty,
                limit_price=int(bid),
                client_order_id=client_order_id,
                time_in_force=tif,
                reduce_only=self.cfg.reduce_only_exits,
                expiration_ts=int(time.time()) + self.cfg.entry_order_timeout_seconds,
                cancel_order_on_pause=self.cfg.cancel_order_on_pause,
            )
            order = resp.get("order", resp)
            order_id = str(order.get("order_id") or resp.get("order_id") or client_order_id)
            status = str(order.get("status") or "pending").lower()
            self.pending_exit_order = PendingOrder(
                order_id=order_id,
                client_order_id=client_order_id,
                ticker=self.active_position.ticker,
                side=side,
                qty=self.active_position.qty,
                submitted_price=int(bid),
                submitted_ts=datetime.now(timezone.utc).isoformat(),
                state=status,
                reason=reason,
                purpose="exit",
                intended_reason=reason,
                filled_qty=int(order.get("fill_count") or 0),
                remaining_qty=int(order.get("remaining_count") or self.active_position.qty),
            )
            self._log_event("exit_submitted", asdict(self.pending_exit_order))
            if status == "resting":
                self._log_event("exit_resting", asdict(self.pending_exit_order))
        except Exception as exc:
            self.order_rejections += 1
            self.api_errors += 1
            self.error_signal.emit(f"Exit order rejected: {exc}")
            self._log_event("exit_rejected", {"ticker": self.active_position.ticker, "reason": str(exc)})

    def _monitor_pending_exit(self, rows: list[dict[str, Any]]) -> None:
        if not self.pending_exit_order or not self.active_position:
            return
        order = self.pending_exit_order
        try:
            status_payload = self.client.get_order_status(order.order_id)
        except Exception as exc:
            self.api_errors += 1
            self.error_signal.emit(f"Exit status refresh failed: {exc}")
            return

        raw = status_payload.get("order", status_payload)
        status = str(raw.get("status") or "pending").lower()
        fill_count = int(raw.get("fill_count") or 0)
        remaining = int(raw.get("remaining_count") or max(0, order.qty - fill_count))

        if status in {"executed", "filled"} or fill_count > 0:
            exit_price = int(order.submitted_price)
            realized = (exit_price - self.active_position.entry_price) * (1 if self.active_position.side == "buy_yes" else -1) * self.active_position.qty
            self.log_signal.emit(f"Exit filled: realized PnL = {'-' if realized < 0 else ''}${abs(realized)/100:.2f}")
            self._log_event("exit_filled", {"order_id": order.order_id, "realized_pnl_cents": realized})
            self._finalize_closed_position(exit_price=exit_price, reason=order.reason, realized_pnl=realized)
            self.pending_exit_order = None
            return

        if status in {"canceled", "cancelled", "rejected"}:
            self.order_rejections += 1
            self.error_signal.emit(f"Exit order not completed: order_id={order.order_id} status={status}")
            self._log_event("exit_rejected", {"order_id": order.order_id, "status": status})
            self.pending_exit_order = None

    def _finalize_closed_position(self, exit_price: int, reason: str, realized_pnl: float) -> None:
        if not self.active_position:
            return
        now = datetime.now(timezone.utc).isoformat()
        trade = SessionTrade(
            ticker=self.active_position.ticker,
            title=self.active_position.title,
            side=self.active_position.side,
            qty=self.active_position.qty,
            entry_price=self.active_position.entry_price,
            exit_price=exit_price,
            realized_pnl=realized_pnl,
            entry_ts=self.active_position.entry_ts,
            exit_ts=now,
            exit_reason=reason,
            resolved=reason == "hold_to_resolution",
        )
        if self.db and self.active_position.trade_id is not None:
            self.db.close_trade(
                trade_id=self.active_position.trade_id,
                exit_price=exit_price,
                exit_ts=now,
                realized_pnl=realized_pnl,
                exit_reason=reason,
                reached_resolution=trade.resolved,
                diagnostics_exit={"status": "closed", "reason": reason},
            )
        self.completed_trades += 1
        if realized_pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        self.history.append(trade)
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
            self.log_signal.emit(f"Balance recheck complete. Cash: ${balance_cents/100:.2f} | Session PnL: {sign}${abs(pnl)/100:.2f}")
            self._log_event("balance_recheck", {"balance_cents": balance_cents, "session_pnl_cents": pnl})
            if pnl <= -self.cfg.session_stop_loss_cents and self.cfg.disable_new_entries_after_stop_hit:
                if not self.new_entries_disabled:
                    self.log_signal.emit(f"Session stop-loss hit at -${self.cfg.session_stop_loss_cents/100:.2f}. New entries disabled.")
                self.new_entries_disabled = True
            if pnl >= self.cfg.session_take_profit_cents and self.cfg.disable_new_entries_after_stop_hit:
                if not self.new_entries_disabled:
                    self.log_signal.emit(f"Session take-profit hit at +${self.cfg.session_take_profit_cents/100:.2f}. New entries disabled.")
                self.new_entries_disabled = True
        except Exception as exc:
            self.api_errors += 1
            self.error_signal.emit(f"Balance refresh failed: {exc}")

    def _log_event(self, event_type: str, details: dict[str, Any]) -> None:
        if not self.db:
            return
        try:
            self.db.log_event(datetime.now(timezone.utc).isoformat(), event_type, details)
        except Exception:
            pass
