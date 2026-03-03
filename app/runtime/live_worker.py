import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QObject, Signal

from app.brokers.kalshi_client import KalshiClient
from app.config.defaults import settings
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
    spot_meta_signal = Signal(str, float)
    selected_market_signal = Signal(str)
    selected_title_signal = Signal(str)
    cash_balance_signal = Signal(int)
    session_pnl_signal = Signal(int)

    def __init__(self, client: KalshiClient, controller, start_balance_cents: int, db, spot_client=None):
        super().__init__()
        self.client = client
        self.controller = controller
        self.db = db
        self.spot_client = spot_client
        self.cfg = settings.global_settings
        self.start_balance_cents = start_balance_cents
        self.current_balance_cents = start_balance_cents
        self._running = False

        self.pending_entry_order = None
        self.pending_exit_order = None
        self.active_position = None
        self.history: list[SessionTrade] = []

        self.completed_trades = 0
        self.consecutive_losses = 0
        self.order_rejections = 0
        self.api_errors = 0
        self.new_entries_disabled = False

        self.last_spot_update_ts: datetime | None = None
        self.last_quote_update_ts: datetime | None = None

        self._last_spot = 0.0
        self._last_series = 0.0
        self._last_eval = 0.0
        self._last_balance = 0.0

    def _new_client_order_id(self, prefix: str, side: str) -> str:
        return f"{prefix}-{side}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    def start(self) -> None:
        self._running = True
        self.loop_status_signal.emit("Loop: running")
        self.log_signal.emit("Live loop started")
        validation = self.client.validate_hourly_series()
        if validation.get("ok"):
            self.log_signal.emit(f"Hourly series validated: {validation.get('ticker')}")
        else:
            self.error_signal.emit(f"Hourly series validation failed for {validation.get('ticker')}: {validation.get('error')}")
        while self._running:
            self.run_cycle()
            time.sleep(1)
        self.loop_status_signal.emit("Loop: stopped")

    def stop(self) -> None:
        self._running = False

    def shutdown_cleanup(self) -> None:
        if self.pending_entry_order:
            try:
                self.client.cancel_order(self.pending_entry_order.order_id)
            except Exception:
                pass
            self.pending_entry_order = None

    def run_cycle(self) -> None:
        now = time.monotonic()
        self.loop_status_signal.emit("Loop: polling")

        if now - self._last_spot >= 3:
            self._refresh_spot()
            self._last_spot = now

        rows = []
        target = None
        if now - self._last_series >= 5:
            rows, target = self._refresh_series_quotes()
            self._last_series = now

        self._monitor_pending_entry(rows)
        self._monitor_pending_exit(rows)

        if now - self._last_eval >= 4:
            self._evaluate_and_trade(rows, target)
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
            self.last_spot_update_ts = datetime.now(timezone.utc)
            self.spot_signal.emit(spot)
            self.spot_meta_signal.emit(self.last_spot_update_ts.isoformat(), self._spot_age_seconds())
        except Exception as exc:
            self.api_errors += 1
            self.error_signal.emit(f"Spot refresh failed: {exc}")

    def _spot_age_seconds(self) -> float:
        if not self.last_spot_update_ts:
            return 9999.0
        return (datetime.now(timezone.utc) - self.last_spot_update_ts).total_seconds()

    def _quote_age_seconds(self) -> float:
        if not self.last_quote_update_ts:
            return 9999.0
        return (datetime.now(timezone.utc) - self.last_quote_update_ts).total_seconds()

    def _refresh_series_quotes(self):
        markets = self.client.get_open_hourly_trade_markets()
        rows = self.client.get_hourly_series_quote_rows(markets)
        self.last_quote_update_ts = datetime.now(timezone.utc)
        target = self.client.resolve_btc_hourly_trade_target_market(
            now=datetime.now(timezone.utc),
            spot_price=self.controller.feed.prices()[-1] if self.controller.feed.prices() else None,
        )

        if not rows:
            self.log_signal.emit(f"No open {self.cfg.btc_hourly_trade_series_ticker} contracts found")
            diag = self.client.last_series_diagnostics or {}
            self.log_signal.emit(f"{self.cfg.btc_hourly_trade_series_ticker} open query returned {diag.get('open_count',0)} rows")
            self.log_signal.emit(
                f"{self.cfg.btc_hourly_trade_series_ticker} no-status query returned {diag.get('fallback_count',0)} rows, {diag.get('tradable_count',0)} currently tradable"
            )
            self.series_rows_signal.emit([])
            return [], None

        target_ticker = target.get("ticker") if target else ""
        target_title = target.get("title") if target else ""
        self.selected_market_signal.emit(target_ticker or "")
        self.selected_title_signal.emit(target_title or "")
        if target:
            self.log_signal.emit(
                f"Selected threshold target: {target_ticker} | threshold={target.get('strike') or 'n/a'} | spot={self.controller.feed.prices()[-1] if self.controller.feed.prices() else 'n/a'}"
            )

        for row in rows:
            row["selected"] = "Yes" if row.get("ticker") == target_ticker else ""
        self.series_rows_signal.emit(rows)
        self.log_signal.emit(f"Series quote refresh complete: {len(rows)} {self.cfg.btc_hourly_trade_series_ticker} contracts updated")
        return rows, target

    def _build_entry_payload(self, target: dict, side: str, price: int, qty: int):
        return {
            "ticker": target["ticker"],
            "side": side,
            "action": "buy",
            "count": qty,
            "limit_price": price,
            "post_only": self.cfg.entry_post_only,
            "time_in_force": self.cfg.entry_time_in_force,
            "cancel_order_on_pause": self.cfg.cancel_order_on_pause,
            "expiration_ts": int(time.time()) + self.cfg.entry_order_timeout_seconds,
        }

    def _evaluate_and_trade(self, rows: list[dict], target: dict | None) -> None:
        if self.active_position:
            self._monitor_active_position(rows)
            return
        if not target:
            return
        target_row = next((r for r in rows if r.get("ticker") == target.get("ticker")), None)
        if not target_row:
            return

        self.log_signal.emit(f"Evaluating target: {target_row['ticker']}")
        spot_age = self._spot_age_seconds()
        quote_age = self._quote_age_seconds()
        self.log_signal.emit(f"Spot freshness: age={spot_age:.1f}s")
        self.log_signal.emit(f"Quote freshness: age={quote_age:.1f}s")

        quote = {
            "yes_bid": target_row.get("yes_bid") or 0,
            "yes_ask": target_row.get("yes_ask") or 0,
            "no_bid": target_row.get("no_bid") or 0,
            "no_ask": target_row.get("no_ask") or 0,
        }

        stale_spot = spot_age > self.cfg.quote_stale_stop_seconds
        stale_quote = quote_age > self.cfg.quote_stale_stop_seconds
        if stale_spot or stale_quote:
            self.log_signal.emit(
                f"Quote/feed stale. Skipping entry. spot_age={spot_age:.1f}s quote_age={quote_age:.1f}s max_allowed={self.cfg.quote_stale_stop_seconds:.1f}s"
            )
            return

        close_dt = datetime.fromisoformat(target_row["close_time"].replace("Z", "+00:00"))
        seconds_to_expiry = int((close_dt - datetime.now(timezone.utc)).total_seconds())
        decision = self.controller.evaluate(quote, max(seconds_to_expiry, 0))
        self.log_signal.emit(f"Decision result: {decision}")
        if decision not in {"buy_yes", "buy_no"}:
            return

        side = "yes" if decision == "buy_yes" else "no"
        price = int(quote["yes_ask"] if side == "yes" else quote["no_ask"])
        payload = self._build_entry_payload(target_row, side, price, 1)
        if self.cfg.dry_run_mode:
            self.log_signal.emit(f"DRY RUN: order qualified but not submitted | payload={payload}")
            return

        client_order_id = self._new_client_order_id("hourbtc", side)
        self.log_signal.emit(
            f"Submitting entry order: ticker={payload['ticker']} side={side} action=buy qty=1 price={price} post_only={self.cfg.entry_post_only} tif={self.cfg.entry_time_in_force}"
        )
        try:
            resp = self.client.place_entry_order(client_order_id=client_order_id, **payload)
            order = resp.get("order", resp)
            self.pending_entry_order = PendingOrder(
                order_id=str(order.get("order_id") or client_order_id),
                client_order_id=client_order_id,
                ticker=payload["ticker"],
                side=side,
                qty=1,
                submitted_price=price,
                submitted_ts=datetime.now(timezone.utc).isoformat(),
                state=str(order.get("status") or "pending").lower(),
                reason="strategy_entry",
                purpose="entry",
                intended_reason="qualified_entry",
            )
            self.log_signal.emit(f"Entry order accepted: order_id={self.pending_entry_order.order_id} status={self.pending_entry_order.state}")
        except Exception as exc:
            self.error_signal.emit(f"Entry order rejected: {exc}")

    def _monitor_pending_entry(self, rows: list[dict]) -> None:
        if not self.pending_entry_order:
            return
        order = self.pending_entry_order
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(order.submitted_ts)).total_seconds()
        status_payload = self.client.get_order_status(order.order_id)
        raw = status_payload.get("order", status_payload)
        status = str(raw.get("status") or "pending").lower()
        fill_count = int(raw.get("fill_count") or 0)
        if status in {"filled", "executed"} or fill_count > 0:
            self.active_position = ActivePosition(
                ticker=order.ticker,
                title=next((r.get("title", "") for r in rows if r.get("ticker") == order.ticker), ""),
                side=f"buy_{order.side}",
                qty=max(fill_count, 1),
                entry_price=order.submitted_price,
                entry_ts=order.submitted_ts,
                order_id=order.order_id,
                status="open",
                exit_rule="invalidation",
                current_exit_ref=float(order.submitted_price),
                unrealized_pnl=0.0,
                close_time=next((r.get("close_time", "") for r in rows if r.get("ticker") == order.ticker), ""),
            )
            self.pending_entry_order = None
            self.log_signal.emit("Entry filled")
            return
        if status in {"canceled", "cancelled", "rejected"}:
            self.pending_entry_order = None
            return
        if status in {"pending", "resting"} and elapsed >= self.cfg.entry_order_timeout_seconds:
            self.log_signal.emit(f"Entry order timed out after {self.cfg.entry_order_timeout_seconds}s, canceling order_id={order.order_id}")
            self.client.cancel_order(order.order_id)
            self.pending_entry_order = None

    def _monitor_active_position(self, rows: list[dict]) -> None:
        if not self.active_position or self.pending_exit_order:
            return
        row = next((r for r in rows if r.get("ticker") == self.active_position.ticker), None)
        if not row:
            return
        side_yes = self.active_position.side == "buy_yes"
        exit_ref = row.get("yes_bid") if side_yes else row.get("no_bid")
        if exit_ref is None:
            return
        self.active_position.current_exit_ref = float(exit_ref)
        fair = compute_fair_value(
            self.controller.feed.prices()[-1],
            row.get("strike") or self.controller.feed.prices()[-1],
            row.get("yes_ask") or self.active_position.entry_price,
            row.get("no_ask") or self.active_position.entry_price,
            self.controller.feed.prices(),
            60,
        )
        regime = fair.reasons[0].split("=")[-1]
        should, reason = should_exit(self.active_position.side, regime, fair, True, abs(self.active_position.unrealized_pnl))
        if should:
            self._submit_exit_for_active_position(reason, row)

    def _submit_exit_for_active_position(self, reason: str, row: dict | None) -> None:
        if not self.active_position or self.pending_exit_order:
            return
        side = "yes" if self.active_position.side == "buy_yes" else "no"
        bid = row.get("yes_bid") if side == "yes" else row.get("no_bid")
        if bid is None:
            return
        client_order_id = self._new_client_order_id("hourbtc-exit", side)
        self.log_signal.emit(
            f"Submitting exit order: ticker={self.active_position.ticker} side={side} action=sell qty={self.active_position.qty} price={int(bid)} reason={reason}"
        )
        if self.cfg.dry_run_mode:
            self.log_signal.emit("DRY RUN: exit qualified but not submitted")
            return
        resp = self.client.place_exit_order(
            ticker=self.active_position.ticker,
            side=side,
            count=self.active_position.qty,
            limit_price=int(bid),
            client_order_id=client_order_id,
            time_in_force=self.cfg.exit_time_in_force,
        )
        order = resp.get("order", resp)
        self.pending_exit_order = PendingOrder(
            order_id=str(order.get("order_id") or client_order_id),
            client_order_id=client_order_id,
            ticker=self.active_position.ticker,
            side=side,
            qty=self.active_position.qty,
            submitted_price=int(bid),
            submitted_ts=datetime.now(timezone.utc).isoformat(),
            state=str(order.get("status") or "pending").lower(),
            reason=reason,
            purpose="exit",
            intended_reason=reason,
        )

    def _monitor_pending_exit(self, rows: list[dict]) -> None:
        if not self.pending_exit_order or not self.active_position:
            return
        order = self.pending_exit_order
        raw = self.client.get_order_status(order.order_id).get("order", {})
        status = str(raw.get("status") or "pending").lower()
        fill_count = int(raw.get("fill_count") or 0)
        if status in {"filled", "executed"} or fill_count > 0:
            realized = (order.submitted_price - self.active_position.entry_price) * (1 if self.active_position.side == "buy_yes" else -1)
            self.log_signal.emit(f"Exit filled: realized PnL = {'-' if realized < 0 else ''}${abs(realized)/100:.2f}")
            self._finalize_closed_position(order.submitted_price, order.reason, realized)
            self.pending_exit_order = None
        elif status in {"canceled", "cancelled", "rejected"}:
            self.pending_exit_order = None
            self.error_signal.emit(f"Exit order not completed: order_id={order.order_id} status={status}")

    def _finalize_closed_position(self, exit_price: int, reason: str, realized_pnl: float) -> None:
        if not self.active_position:
            return
        self.history.append(
            SessionTrade(
                ticker=self.active_position.ticker,
                title=self.active_position.title,
                side=self.active_position.side,
                qty=self.active_position.qty,
                entry_price=self.active_position.entry_price,
                exit_price=exit_price,
                realized_pnl=realized_pnl,
                entry_ts=self.active_position.entry_ts,
                exit_ts=datetime.now(timezone.utc).isoformat(),
                exit_reason=reason,
                resolved=reason == "hold_to_resolution",
            )
        )
        self.active_position = None

    def _refresh_balance(self) -> None:
        summary = self.client.get_account_summary()
        balance_cents = int(float(summary.get("balance") or self.current_balance_cents))
        self.current_balance_cents = balance_cents
        pnl = balance_cents - self.start_balance_cents
        self.cash_balance_signal.emit(balance_cents)
        self.session_pnl_signal.emit(pnl)
