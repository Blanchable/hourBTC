import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from PySide6.QtCore import QObject, Signal

from app.brokers.kalshi_client import KalshiClient, KalshiRateLimitError
from app.config.defaults import settings
from app.config.runtime_settings import RuntimeSettings, from_defaults


@dataclass
class PendingOrder:
    order_id: str
    client_order_id: str
    ticker: str
    side: str
    qty: int
    submitted_price: int
    submitted_ts: str


class LiveWorker(QObject):
    finished_signal = Signal()
    log_signal = Signal(str)
    error_signal = Signal(str)
    loop_status_signal = Signal(str)
    series_rows_signal = Signal(list)
    selected_market_signal = Signal(str)
    selected_title_signal = Signal(str)
    spot_signal = Signal(float)
    spot_meta_signal = Signal(str, float)
    cash_balance_signal = Signal(int)
    session_pnl_signal = Signal(int)

    def __init__(self, client: KalshiClient, controller, start_balance_cents: int, db, spot_client=None, runtime_settings: RuntimeSettings | None = None):
        super().__init__()
        self.client = client
        self.controller = controller
        self.db = db
        self.spot_client = spot_client
        self.runtime_settings = runtime_settings or from_defaults(settings.global_settings)

        self.start_balance_cents = start_balance_cents
        self.current_balance_cents = start_balance_cents
        self.stop_requested = False

        self.pending_entry_order: PendingOrder | None = None
        self.last_spot_ts: datetime | None = None
        self.last_quote_ts: datetime | None = None
        self.last_spot: float | None = None
        self.last_rows: list[dict] = []
        self.last_target: dict | None = None

        self._last_spot_poll = 0.0
        self._last_market_refresh = 0.0
        self._last_eval = 0.0
        self._last_balance = 0.0

        self._backoff_until = 0.0
        self._backoff_seconds = self.runtime_settings.rate_limit_backoff_seconds
        self._backoff_logged = False

    def apply_runtime_settings(self, new_settings: RuntimeSettings) -> None:
        self.runtime_settings = new_settings
        self.log_signal.emit("Settings applied to running bot")

    def get_cached_shadow_context(self) -> dict:
        return {
            "rows": self.last_rows,
            "target": self.last_target,
            "spot": self.last_spot,
            "spot_age": self._spot_age(),
            "quote_age": self._quote_age(),
            "diagnostics": self.client.last_series_diagnostics,
        }

    def _new_client_order_id(self, prefix: str, side: str) -> str:
        return f"{prefix}-{side}-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"

    def start(self) -> None:
        self.stop_requested = False
        self.loop_status_signal.emit("Loop: running")
        self.log_signal.emit("Live loop started")
        while not self.stop_requested:
            try:
                self.run_cycle()
            except KalshiRateLimitError as exc:
                self._begin_backoff(str(exc))
            except Exception as exc:
                if self.stop_requested:
                    self.log_signal.emit(f"Stopping with in-flight transport error: {exc}")
                    break
                self.error_signal.emit(f"Loop error: {exc}")
            time.sleep(1)
        self.loop_status_signal.emit("Loop: stopped")
        self.finished_signal.emit()

    def stop(self) -> None:
        self.stop_requested = True

    def shutdown_cleanup(self) -> None:
        if self.pending_entry_order:
            try:
                self.client.cancel_order(self.pending_entry_order.order_id)
            except Exception:
                pass
            self.pending_entry_order = None

    def _begin_backoff(self, msg: str) -> None:
        now = time.monotonic()
        self._backoff_until = now + self._backoff_seconds
        self.log_signal.emit(f"Rate limited by Kalshi. Backing off for {self._backoff_seconds}s")
        self._backoff_seconds = min(self.runtime_settings.rate_limit_backoff_max_seconds, self._backoff_seconds * 2)
        self._backoff_logged = True
        self.error_signal.emit(msg)

    def _spot_age(self) -> float:
        if not self.last_spot_ts:
            return 9999.0
        return (datetime.now(timezone.utc) - self.last_spot_ts).total_seconds()

    def _quote_age(self) -> float:
        if not self.last_quote_ts:
            return 9999.0
        return (datetime.now(timezone.utc) - self.last_quote_ts).total_seconds()

    def run_cycle(self) -> None:
        if self.stop_requested:
            return
        now = time.monotonic()
        if now < self._backoff_until:
            return
        if self._backoff_logged:
            self.log_signal.emit("Rate-limit backoff ended. Resuming normal polling.")
            self._backoff_logged = False
            self._backoff_seconds = self.runtime_settings.rate_limit_backoff_seconds

        self.loop_status_signal.emit("Loop: polling")

        if now - self._last_spot_poll >= 3 and not self.stop_requested:
            self._refresh_spot()
            self._last_spot_poll = now

        if now - self._last_market_refresh >= self.runtime_settings.market_list_refresh_seconds and not self.stop_requested:
            self._refresh_market_snapshot()
            self._last_market_refresh = now

        if now - self._last_eval >= 4 and not self.stop_requested:
            self._evaluate_target_once()
            self._last_eval = now

        if now - self._last_balance >= 25 and not self.stop_requested:
            self._refresh_balance()
            self._last_balance = now

    def _refresh_spot(self) -> None:
        try:
            spot = self.spot_client.fetch_btc_spot()
            self.controller.feed.push(spot)
            self.last_spot = spot
            self.last_spot_ts = datetime.now(timezone.utc)
            self.spot_signal.emit(spot)
            self.spot_meta_signal.emit(self.last_spot_ts.isoformat(), self._spot_age())
        except Exception as exc:
            self.error_signal.emit(f"Spot refresh failed: {exc}")

    def _refresh_market_snapshot(self) -> None:
        now_dt = datetime.now(timezone.utc)
        markets = self.client.get_live_candidate_trade_markets(
            now=now_dt,
            fallback_window_hours=self.runtime_settings.fallback_window_hours,
            cache_ttl_seconds=self.runtime_settings.market_list_refresh_seconds,
        )
        if not markets:
            self.log_signal.emit("No live KXBTCD trade candidates found")
            diag = self.client.last_series_diagnostics or {}
            self.log_signal.emit(f"KXBTCD open query returned {diag.get('open_query_count', 0)} rows")
            self.log_signal.emit(
                f"KXBTCD fallback window returned {diag.get('fallback_query_count', 0)} rows, {diag.get('candidate_count', 0)} live candidates"
            )
            self.series_rows_signal.emit([])
            self.last_rows = []
            self.last_target = None
            return

        target = self.client.resolve_btc_hourly_trade_target_market(now=now_dt, spot_price=self.last_spot, markets=markets)
        self.last_target = target
        self.selected_market_signal.emit(target.get("ticker", "") if target else "")
        self.selected_title_signal.emit(target.get("title", "") if target else "")

        diag = self.client.last_series_diagnostics or {}
        if diag.get("used_fallback"):
            self.log_signal.emit("KXBTCD open query returned 0 rows; trying bounded fallback")
            self.log_signal.emit(f"KXBTCD fallback window returned {diag.get('fallback_query_count', 0)} rows")
            self.log_signal.emit(f"Live trade candidates after filtering: {diag.get('candidate_count', 0)}")

        selected_idx = 0
        if target:
            tickers = [m.get("ticker") for m in markets]
            if target.get("ticker") in tickers:
                selected_idx = tickers.index(target.get("ticker"))
        subset = markets[max(0, selected_idx - 1): selected_idx + 2]
        rows = self.client.get_hourly_series_quote_rows(subset)
        if target:
            for r in rows:
                r["selected"] = "Yes" if r.get("ticker") == target.get("ticker") else ""
        self.series_rows_signal.emit(rows)
        self.last_rows = rows
        self.last_quote_ts = datetime.now(timezone.utc)
        self.log_signal.emit(f"Series quote refresh complete: {len(rows)} KXBTCD contracts updated")
        if target:
            self.log_signal.emit(f"Selected threshold target: {target.get('ticker')}")
            self.log_signal.emit(f"Selected title: {target.get('title')}")

    def _evaluate_target_once(self) -> None:
        if not self.last_target:
            return
        row = next((r for r in self.last_rows if r.get("ticker") == self.last_target.get("ticker")), None)
        if not row:
            return
        self.log_signal.emit(f"Evaluating target: {row.get('ticker')}")
        spot_age = self._spot_age()
        quote_age = self._quote_age()
        self.log_signal.emit(f"Spot freshness: age={spot_age:.1f}s")
        self.log_signal.emit(f"Quote freshness: age={quote_age:.1f}s")
        if spot_age > self.runtime_settings.quote_stale_stop_seconds or quote_age > self.runtime_settings.quote_stale_stop_seconds:
            self.log_signal.emit(
                f"Quote/feed stale. Skipping entry. spot_age={spot_age:.1f}s max_allowed={self.runtime_settings.quote_stale_stop_seconds:.1f}s quote_age={quote_age:.1f}s"
            )
            return
        quote = {"yes_bid": row.get("yes_bid") or 0, "yes_ask": row.get("yes_ask") or 0, "no_bid": row.get("no_bid") or 0, "no_ask": row.get("no_ask") or 0}
        decision = self.controller.evaluate(quote, 1200)
        self.log_signal.emit(f"Decision result: {decision}")
        if decision not in {"buy_yes", "buy_no"}:
            return
        side = "yes" if decision == "buy_yes" else "no"
        price = int(quote["yes_ask"] if side == "yes" else quote["no_ask"])
        payload = {
            "ticker": row["ticker"],
            "side": side,
            "action": "buy",
            "count": 1,
            "limit_price": price,
            "post_only": self.runtime_settings.entry_post_only,
            "time_in_force": settings.global_settings.entry_time_in_force,
            "expiration_ts": int(time.time()) + self.runtime_settings.entry_order_timeout_seconds,
        }
        if self.runtime_settings.dry_run_mode:
            self.log_signal.emit(f"DRY RUN: order qualified but not submitted | payload={payload}")
            return

        order = self.client.place_entry_order(client_order_id=self._new_client_order_id("hourbtc", side), **payload)
        o = order.get("order", order)
        self.pending_entry_order = PendingOrder(
            order_id=str(o.get("order_id")),
            client_order_id=o.get("client_order_id", ""),
            ticker=row["ticker"],
            side=side,
            qty=1,
            submitted_price=price,
            submitted_ts=datetime.now(timezone.utc).isoformat(),
        )
        self.log_signal.emit(f"Entry order accepted: order_id={self.pending_entry_order.order_id} status={o.get('status')}")

    def _refresh_balance(self) -> None:
        s = self.client.get_account_summary()
        bal = int(float(s.get("balance") or self.current_balance_cents))
        self.current_balance_cents = bal
        self.cash_balance_signal.emit(bal)
        self.session_pnl_signal.emit(bal - self.start_balance_cents)
