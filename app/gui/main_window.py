import logging
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QWidget,
)

from app.brokers.kalshi_client import KalshiClient
from app.config.defaults import settings
from app.config.secrets import CredentialProfile, SecretStore
from app.core.controller import Controller
from app.feeds.btc_feed import BTCFeed
from app.feeds.spot_client import SpotClient
from app.runtime.live_worker import LiveWorker
from app.storage.db import DB

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECRETS_PATH = PROJECT_ROOT / "data" / "secrets.json"
DB_PATH = PROJECT_ROOT / "data" / "hourbtc.db"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.client = None
        self.controller = None
        self.worker = None
        self.worker_thread = None
        self.loop_running = False

        self.secret_store = SecretStore(SECRETS_PATH)
        self.db = DB(DB_PATH)
        self.db.bootstrap()

        self.setWindowTitle("hourBTC Kalshi 1H Bot")
        root = QWidget()
        layout = QGridLayout(root)

        self.connection_status_label = QLabel("Not connected")
        self.cash_balance_label = QLabel("Cash Available: --")
        self.session_pnl_label = QLabel("Session PnL: $0.00")
        self.loop_status_label = QLabel("Loop: idle")
        self.spot_label = QLabel("BTC Spot: --")
        self.last_spot_update_label = QLabel("Last Spot Update: --")
        self.feed_age_label = QLabel("Feed Age: --")
        self.mode_label = QLabel(f"Mode: {'DRY RUN' if settings.global_settings.dry_run_mode else 'LIVE'}")
        self.trade_series_label = QLabel(f"Trade Series: {settings.global_settings.btc_hourly_trade_series_ticker}")
        self.display_series_label = QLabel(f"Display Series: {settings.global_settings.btc_hourly_range_series_ticker}")
        self.target_family_label = QLabel("Selected Target Family: Threshold")
        self.selected_market_label = QLabel("Selected Market: --")
        self.selected_title_label = QLabel("Selected Title: --")

        self.env = QLineEdit("paper")
        self.api_key = QLineEdit()
        self.key_file = QLineEdit()

        self.browse_button = QPushButton("Pick .key")
        self.browse_button.clicked.connect(self.pick_key)
        self.save_button = QPushButton("Save Credentials")
        self.start_button = QPushButton("Start Bot")
        self.stop_button = QPushButton("Stop Bot")
        self.shadow_button = QPushButton("Shadow Order Test")
        self.smoke_button = QPushButton("Paper Smoke Test")
        self.save_button.clicked.connect(self.save_credentials)
        self.start_button.clicked.connect(self.start_bot)
        self.stop_button.clicked.connect(self.stop_bot)
        self.shadow_button.clicked.connect(self.shadow_order_test)
        self.smoke_button.clicked.connect(self.paper_smoke_test)

        layout.addWidget(self.connection_status_label, 0, 0)
        layout.addWidget(self.cash_balance_label, 0, 1)
        layout.addWidget(self.session_pnl_label, 0, 2)
        layout.addWidget(self.loop_status_label, 1, 0)
        layout.addWidget(self.spot_label, 1, 1)
        layout.addWidget(self.mode_label, 1, 2)
        layout.addWidget(self.trade_series_label, 2, 0)
        layout.addWidget(self.display_series_label, 2, 1)
        layout.addWidget(self.target_family_label, 2, 2)
        layout.addWidget(self.last_spot_update_label, 3, 0)
        layout.addWidget(self.feed_age_label, 3, 1)
        layout.addWidget(self.selected_market_label, 3, 2)
        layout.addWidget(self.selected_title_label, 4, 0, 1, 3)

        layout.addWidget(QLabel("Environment (paper/production)"), 5, 0)
        layout.addWidget(self.env, 5, 1)
        layout.addWidget(QLabel("API key ID"), 6, 0)
        layout.addWidget(self.api_key, 6, 1)
        layout.addWidget(QLabel("Private key file"), 7, 0)
        layout.addWidget(self.key_file, 7, 1)
        layout.addWidget(self.browse_button, 7, 2)
        layout.addWidget(self.save_button, 8, 0)
        layout.addWidget(self.start_button, 8, 1)
        layout.addWidget(self.stop_button, 8, 2)
        layout.addWidget(self.shadow_button, 9, 0)
        layout.addWidget(self.smoke_button, 9, 1)

        self.tabs = QTabWidget()
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.series_table = QTableWidget(0, 11)
        self.series_table.setHorizontalHeaderLabels(
            ["Ticker", "Title", "Close Time", "Strike", "YES Bid", "YES Ask", "NO Bid", "NO Ask", "Mid / Fair Ref", "Selected", "Last Update"]
        )
        self.active_positions_table = QTableWidget(0, 10)
        self.active_positions_table.setHorizontalHeaderLabels(
            ["Ticker", "Side", "Qty", "Entry Price", "Current Exit Ref", "Unrealized PnL", "Status", "Entry Time", "Order ID", "Exit Rule"]
        )
        self.session_history_table = QTableWidget(0, 10)
        self.session_history_table.setHorizontalHeaderLabels(
            ["Ticker", "Side", "Qty", "Entry Price", "Exit Price", "Realized PnL", "Entry Time", "Exit Time", "Exit Reason", "Resolved"]
        )

        self.tabs.addTab(self.logs, "Logs")
        self.tabs.addTab(self.series_table, "Series Order Book")
        self.tabs.addTab(self.active_positions_table, "Active Positions")
        self.tabs.addTab(self.session_history_table, "Session History")
        layout.addWidget(self.tabs, 10, 0, 1, 3)

        self.setCentralWidget(root)
        self.append_log("Application started.")
        self._load_credentials_for_env(self.env.text().strip().lower())

    def append_log(self, message: str) -> None:
        self.logs.append(message)
        self.logger.info(message)

    def append_error(self, message: str) -> None:
        self.logs.append(f"ERROR: {message}")
        self.logger.error(message)

    def pick_key(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "Select private key", "", "Key Files (*.key *.pem)")
        if file_name:
            self.key_file.setText(file_name)

    def _normalize_env(self) -> str:
        return self.env.text().strip().lower()

    def _load_credentials_for_env(self, env: str) -> None:
        if env in {"paper", "production"}:
            p = self.secret_store.load(env)
            if p.api_key_id:
                self.api_key.setText(p.api_key_id)
            if p.private_key_path:
                self.key_file.setText(p.private_key_path)

    def save_credentials(self) -> None:
        env = self._normalize_env()
        if env not in {"paper", "production"}:
            self.append_error("environment must be paper/production")
            return
        self.secret_store.save(env, CredentialProfile(api_key_id=self.api_key.text().strip(), private_key_path=self.key_file.text().strip()))
        self.append_log(f"Saved credentials for {env}")

    def _format_cents_as_dollars(self, cents: int | float) -> str:
        return f"${float(cents)/100:.2f}"

    def _extract_available_balance_cents(self, summary: dict) -> int | None:
        bal = summary.get("balance") if isinstance(summary, dict) else None
        try:
            return int(float(bal)) if bal is not None else None
        except Exception:
            return None

    def _validate_inputs(self):
        env = self._normalize_env()
        api_key = self.api_key.text().strip()
        key_file = self.key_file.text().strip()
        if env not in {"paper", "production"}:
            self.append_error("Environment must be paper or production")
            return None
        if not api_key or not key_file or not Path(key_file).exists():
            self.append_error("Missing API key or valid key file")
            return None
        return env, api_key, key_file

    def update_spot_meta(self, ts: str, age: float) -> None:
        self.last_spot_update_label.setText(f"Last Spot Update: {ts}")
        self.feed_age_label.setText(f"Feed Age: {age:.1f}s")

    def update_series_orderbook(self, rows: list[dict]) -> None:
        self.series_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            mid = ((r["yes_bid"] + r["yes_ask"]) / 2) if r.get("yes_bid") is not None and r.get("yes_ask") is not None else None
            vals = [r.get("ticker",""), r.get("title",""), r.get("close_time",""), r.get("strike","--") if r.get("strike") is not None else "--", r.get("yes_bid","--") if r.get("yes_bid") is not None else "--", r.get("yes_ask","--") if r.get("yes_ask") is not None else "--", r.get("no_bid","--") if r.get("no_bid") is not None else "--", r.get("no_ask","--") if r.get("no_ask") is not None else "--", f"{mid:.2f}" if mid is not None else "--", r.get("selected",""), r.get("last_update","")]
            for c,v in enumerate(vals):
                self.series_table.setItem(i,c,QTableWidgetItem(str(v)))

    def update_active_positions(self, rows: list[dict]) -> None:
        self.active_positions_table.setRowCount(len(rows))

    def update_session_history(self, rows: list[dict]) -> None:
        self.session_history_table.setRowCount(len(rows))

    def update_spot(self, price: float) -> None:
        self.spot_label.setText(f"BTC Spot: ${price:,.2f}")

    def update_selected_market(self, ticker: str) -> None:
        self.selected_market_label.setText(f"Selected Market: {ticker or '--'}")

    def update_selected_title(self, title: str) -> None:
        self.selected_title_label.setText(f"Selected Title: {title or '--'}")

    def update_cash_balance(self, cents: int) -> None:
        self.cash_balance_label.setText(f"Cash Available: {self._format_cents_as_dollars(cents)}")

    def update_session_pnl(self, cents: int) -> None:
        sign = '-' if cents < 0 else ''
        self.session_pnl_label.setText(f"Session PnL: {sign}{self._format_cents_as_dollars(abs(cents))}")

    def set_loop_status(self, text: str) -> None:
        self.loop_status_label.setText(text)

    def start_bot(self) -> None:
        if self.loop_running:
            return
        validated = self._validate_inputs()
        if not validated:
            return
        env, api_key, key_file = validated
        self.save_credentials()
        self.client = KalshiClient(env, api_key, key_file)
        summary = self.client.get_account_summary()
        bal = self._extract_available_balance_cents(summary)
        if bal is None:
            self.append_error("Could not read balance")
            return
        self.controller = Controller(self.client, BTCFeed())
        self.controller.connect()
        worker = LiveWorker(self.client, self.controller, bal, self.db, spot_client=SpotClient())
        self.worker = worker
        self.worker_thread = QThread(self)
        worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(worker.start)
        worker.log_signal.connect(self.append_log)
        worker.error_signal.connect(self.append_error)
        worker.loop_status_signal.connect(self.set_loop_status)
        worker.series_rows_signal.connect(self.update_series_orderbook)
        worker.active_positions_signal.connect(self.update_active_positions)
        worker.history_signal.connect(self.update_session_history)
        worker.spot_signal.connect(self.update_spot)
        worker.spot_meta_signal.connect(self.update_spot_meta)
        worker.selected_market_signal.connect(self.update_selected_market)
        worker.selected_title_signal.connect(self.update_selected_title)
        worker.cash_balance_signal.connect(self.update_cash_balance)
        worker.session_pnl_signal.connect(self.update_session_pnl)
        self.loop_running = True
        self.connection_status_label.setText("Connected!")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.worker_thread.start()

    def stop_bot(self) -> None:
        if self.worker:
            self.worker.stop()
        if self.worker_thread:
            self.worker_thread.quit()
            self.worker_thread.wait(5000)
        if self.worker:
            self.worker.shutdown_cleanup()
        self.worker = None
        self.worker_thread = None
        self.loop_running = False
        if self.client:
            self.client.close()
            self.client = None
        self.connection_status_label.setText("Not connected")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.append_log("Live loop stopped")

    def shadow_order_test(self) -> None:
        self.append_log("Shadow test started")
        validated = self._validate_inputs()
        if not validated:
            return
        env, api_key, key_file = validated
        client = KalshiClient(env, api_key, key_file)
        try:
            self.append_log(f"Trade series: {settings.global_settings.btc_hourly_trade_series_ticker}")
            markets = client.get_open_hourly_trade_markets()
            spot = SpotClient().fetch_btc_spot()
            target = client.resolve_btc_hourly_trade_target_market(spot_price=spot)
            if not target:
                self.append_log("No threshold target found")
                return
            self.append_log(f"Selected threshold target: {target['ticker']}")
            self.append_log(f"Target title: {target.get('title','')}")
            self.append_log(f"Spot: {spot}")
            row = client.get_hourly_series_quote_rows([target])[0]
            self.append_log(
                f"Orderbook: yes_bid={row.get('yes_bid')} yes_ask={row.get('yes_ask')} no_bid={row.get('no_bid')} no_ask={row.get('no_ask')}"
            )
            decision = "no_trade"
            if self.controller:
                q = {"yes_bid": row.get("yes_bid") or 0, "yes_ask": row.get("yes_ask") or 0, "no_bid": row.get("no_bid") or 0, "no_ask": row.get("no_ask") or 0}
                decision = self.controller.evaluate(q, 1200)
            self.append_log(f"Strategy decision: {decision}")
            side = "yes" if decision == "buy_yes" else "no"
            price = int((row.get("yes_ask") or 1) if side == "yes" else (row.get("no_ask") or 1))
            payload = {
                "ticker": target["ticker"],
                "side": side,
                "action": "buy",
                "count": 1,
                "limit_price": price,
                "post_only": settings.global_settings.entry_post_only,
                "time_in_force": settings.global_settings.entry_time_in_force,
            }
            self.append_log(f"Would submit order: {payload}")
        finally:
            client.close()

    def paper_smoke_test(self) -> None:
        if self._normalize_env() != "paper":
            self.append_log("Paper Smoke Test is disabled in production.")
            return
        self.append_log("Paper smoke test started")
        validated = self._validate_inputs()
        if not validated:
            return
        env, api_key, key_file = validated
        client = KalshiClient(env, api_key, key_file)
        try:
            spot = SpotClient().fetch_btc_spot()
            target = client.resolve_btc_hourly_trade_target_market(spot_price=spot)
            if not target:
                self.append_error("No threshold target found for smoke test")
                return
            row = client.get_hourly_series_quote_rows([target])[0]
            yes_bid = int(row.get("yes_bid") or 10)
            safe_price = max(1, yes_bid - 20)
            cid = f"smoke-{int(Path(__file__).stat().st_mtime)}"
            self.append_log(f"Selected threshold target: {target['ticker']}")
            self.append_log(f"Submitting safe resting test order: side=yes price={safe_price}")
            resp = client.place_entry_order(
                ticker=target["ticker"],
                side="yes",
                count=1,
                limit_price=safe_price,
                client_order_id=cid,
                post_only=True,
                time_in_force="good_till_canceled",
            )
            order = resp.get("order", resp)
            oid = order.get("order_id")
            self.append_log(f"Smoke test order accepted: order_id={oid} status={order.get('status')}")
            client.cancel_order(str(oid))
            self.append_log("Smoke test order canceled successfully")
        except Exception as exc:
            self.append_error(str(exc))
        finally:
            client.close()

    def closeEvent(self, event):  # noqa: N802
        self.stop_bot()
        super().closeEvent(event)
