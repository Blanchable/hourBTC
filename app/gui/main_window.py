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
from app.config.secrets import CredentialProfile, SecretStore
from app.core.controller import Controller
from app.feeds.btc_feed import BTCFeed
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
        self.session_start_balance_cents = None

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
        self.selected_market_label = QLabel("Selected Market: --")

        self.env = QLineEdit("paper")
        self.api_key = QLineEdit()
        self.key_file = QLineEdit()

        self.browse_button = QPushButton("Pick .key")
        self.browse_button.clicked.connect(self.pick_key)
        self.save_button = QPushButton("Save Credentials")
        self.start_button = QPushButton("Start Bot")
        self.stop_button = QPushButton("Stop Bot")
        self.save_button.clicked.connect(self.save_credentials)
        self.start_button.clicked.connect(self.start_bot)
        self.stop_button.clicked.connect(self.stop_bot)

        layout.addWidget(self.connection_status_label, 0, 0)
        layout.addWidget(self.cash_balance_label, 0, 1)
        layout.addWidget(self.session_pnl_label, 0, 2)
        layout.addWidget(self.loop_status_label, 1, 0)
        layout.addWidget(self.spot_label, 1, 1)
        layout.addWidget(self.selected_market_label, 1, 2)

        layout.addWidget(QLabel("Environment (paper/production)"), 2, 0)
        layout.addWidget(self.env, 2, 1)
        layout.addWidget(QLabel("API key ID"), 3, 0)
        layout.addWidget(self.api_key, 3, 1)
        layout.addWidget(QLabel("Private key file"), 4, 0)
        layout.addWidget(self.key_file, 4, 1)
        layout.addWidget(self.browse_button, 4, 2)
        layout.addWidget(self.save_button, 5, 0)
        layout.addWidget(self.start_button, 5, 1)
        layout.addWidget(self.stop_button, 5, 2)

        self.tabs = QTabWidget()
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.series_table = QTableWidget(0, 11)
        self.series_table.setHorizontalHeaderLabels(
            [
                "Ticker",
                "Title",
                "Close Time",
                "Strike",
                "YES Bid",
                "YES Ask",
                "NO Bid",
                "NO Ask",
                "Mid / Fair Ref",
                "Selected",
                "Last Update",
            ]
        )
        self.active_positions_table = QTableWidget(0, 10)
        self.active_positions_table.setHorizontalHeaderLabels(
            [
                "Ticker",
                "Side",
                "Qty",
                "Entry Price",
                "Current Exit Ref",
                "Unrealized PnL",
                "Status",
                "Entry Time",
                "Order ID",
                "Exit Rule",
            ]
        )
        self.session_history_table = QTableWidget(0, 10)
        self.session_history_table.setHorizontalHeaderLabels(
            [
                "Ticker",
                "Side",
                "Qty",
                "Entry Price",
                "Exit Price",
                "Realized PnL",
                "Entry Time",
                "Exit Time",
                "Exit Reason",
                "Resolved",
            ]
        )

        self.tabs.addTab(self.logs, "Logs")
        self.tabs.addTab(self.series_table, "Series Order Book")
        self.tabs.addTab(self.active_positions_table, "Active Positions")
        self.tabs.addTab(self.session_history_table, "Session History")
        layout.addWidget(self.tabs, 6, 0, 1, 3)

        self.setCentralWidget(root)

        self.set_disconnected_state()
        self.set_loop_status("Loop: idle")
        self.append_log("Application started.")
        self._load_credentials_for_env(self.env.text().strip().lower())

    def pick_key(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "Select private key", "", "Key Files (*.key *.pem)")
        if file_name:
            self.key_file.setText(file_name)

    def append_log(self, message: str) -> None:
        self.logs.append(message)
        self.logger.info(message)

    def append_error(self, message: str) -> None:
        self.logs.append(f"ERROR: {message}")
        self.logger.error(message)

    def update_cash_balance(self, cents: int) -> None:
        self.cash_balance_label.setText(f"Cash Available: {self._format_cents_as_dollars(cents)}")

    def update_session_pnl(self, cents: int) -> None:
        sign = "-" if cents < 0 else ""
        self.session_pnl_label.setText(f"Session PnL: {sign}{self._format_cents_as_dollars(abs(cents))}")

    def update_spot(self, price: float) -> None:
        self.spot_label.setText(f"BTC Spot: ${price:,.2f}")

    def update_selected_market(self, ticker: str) -> None:
        self.selected_market_label.setText(f"Selected Market: {ticker or '--'}")

    def set_loop_status(self, text: str) -> None:
        self.loop_status_label.setText(text)

    def set_connected_state(self, cash_display: str) -> None:
        self.connection_status_label.setText("Connected!")
        self.connection_status_label.setStyleSheet("color: #117a37; font-weight: bold;")
        self.cash_balance_label.setText(f"Cash Available: {cash_display}")

    def set_disconnected_state(self, reason: str | None = None) -> None:
        self.connection_status_label.setText("Not connected")
        self.connection_status_label.setStyleSheet("color: #b00020; font-weight: bold;")
        self.cash_balance_label.setText("Cash Available: --")
        if reason:
            self.append_error(reason)
        self.start_button.setEnabled(not self.loop_running)
        self.stop_button.setEnabled(self.loop_running)

    def update_series_orderbook(self, rows: list[dict]) -> None:
        self.series_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            mid = None
            if row.get("yes_bid") is not None and row.get("yes_ask") is not None:
                mid = (float(row["yes_bid"]) + float(row["yes_ask"])) / 2
            vals = [
                row.get("ticker", ""),
                row.get("title", ""),
                row.get("close_time", ""),
                row.get("strike", "--") if row.get("strike") is not None else "--",
                row.get("yes_bid", "--") if row.get("yes_bid") is not None else "--",
                row.get("yes_ask", "--") if row.get("yes_ask") is not None else "--",
                row.get("no_bid", "--") if row.get("no_bid") is not None else "--",
                row.get("no_ask", "--") if row.get("no_ask") is not None else "--",
                f"{mid:.2f}" if mid is not None else "--",
                row.get("selected", ""),
                row.get("last_update", ""),
            ]
            for c, v in enumerate(vals):
                self.series_table.setItem(i, c, QTableWidgetItem(str(v)))

    def update_active_positions(self, rows: list[dict]) -> None:
        self.active_positions_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            vals = [
                row.get("ticker", ""),
                row.get("side", ""),
                row.get("qty", ""),
                row.get("entry_price", ""),
                row.get("current_exit_ref", ""),
                row.get("unrealized_pnl", ""),
                row.get("status", ""),
                row.get("entry_ts", ""),
                row.get("order_id", ""),
                row.get("exit_rule", ""),
            ]
            for c, v in enumerate(vals):
                self.active_positions_table.setItem(i, c, QTableWidgetItem(str(v)))

    def update_session_history(self, rows: list[dict]) -> None:
        self.session_history_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            vals = [
                row.get("ticker", ""),
                row.get("side", ""),
                row.get("qty", ""),
                row.get("entry_price", ""),
                row.get("exit_price", ""),
                row.get("realized_pnl", ""),
                row.get("entry_ts", ""),
                row.get("exit_ts", ""),
                row.get("exit_reason", ""),
                row.get("resolved", ""),
            ]
            for c, v in enumerate(vals):
                self.session_history_table.setItem(i, c, QTableWidgetItem(str(v)))

    def _normalize_env(self) -> str:
        return self.env.text().strip().lower()

    def _load_credentials_for_env(self, env: str) -> None:
        if env not in {"paper", "production"}:
            return
        profile = self.secret_store.load(env)
        if not self.api_key.text().strip():
            self.api_key.setText(profile.api_key_id)
        if not self.key_file.text().strip():
            self.key_file.setText(profile.private_key_path)
        if profile.api_key_id or profile.private_key_path:
            self.append_log(f"Loaded saved credentials for {env}")

    def _validate_inputs(self, env: str, api_key_id: str, key_file: str) -> bool:
        if env not in {"paper", "production"}:
            self.append_error("Environment must be 'paper' or 'production'.")
            return False
        if not api_key_id:
            self.append_error("API key ID is required.")
            return False
        if not key_file:
            self.append_error("Private key path is required.")
            return False
        if not Path(key_file).exists():
            self.append_error(f"Private key file not found: {key_file}")
            return False
        return True

    def save_credentials(self) -> None:
        env = self._normalize_env()
        if env not in {"paper", "production"}:
            self.append_error("Cannot save credentials: environment must be paper or production.")
            return
        profile = CredentialProfile(api_key_id=self.api_key.text().strip(), private_key_path=self.key_file.text().strip())
        self.secret_store.save(env, profile)
        self.append_log(f"Saved credentials for {env}")

    def _extract_available_balance_cents(self, summary: dict) -> int | None:
        if not isinstance(summary, dict):
            return None
        balance = summary.get("balance")
        if balance is None and isinstance(summary.get("portfolio"), dict):
            balance = summary["portfolio"].get("balance")
        if balance is None:
            return None
        try:
            return int(float(balance))
        except (TypeError, ValueError):
            return None

    def _format_cents_as_dollars(self, cents: int | float) -> str:
        return f"${float(cents) / 100:.2f}"

    def start_bot(self) -> None:
        if self.loop_running:
            self.append_log("Bot already running")
            return
        env = self._normalize_env()
        api_key_id = self.api_key.text().strip()
        key_file = self.key_file.text().strip()
        if not self._validate_inputs(env, api_key_id, key_file):
            return

        self.save_credentials()
        try:
            self.client = KalshiClient(env, api_key_id, key_file)
            summary = self.client.get_account_summary()
            balance_cents = self._extract_available_balance_cents(summary)
            if balance_cents is None:
                raise RuntimeError("balance not found in account summary")
            self.session_start_balance_cents = balance_cents
            self.update_session_pnl(0)
            self.update_cash_balance(balance_cents)
            self.set_connected_state(self._format_cents_as_dollars(balance_cents))
            self.controller = Controller(self.client, BTCFeed())
            self.controller.connect()

            self.worker = LiveWorker(self.client, self.controller, balance_cents, self.db)
            self.worker_thread = QThread(self)
            self.worker.moveToThread(self.worker_thread)
            self.worker_thread.started.connect(self.worker.start)
            self.worker.log_signal.connect(self.append_log)
            self.worker.error_signal.connect(self.append_error)
            self.worker.loop_status_signal.connect(self.set_loop_status)
            self.worker.series_rows_signal.connect(self.update_series_orderbook)
            self.worker.active_positions_signal.connect(self.update_active_positions)
            self.worker.history_signal.connect(self.update_session_history)
            self.worker.spot_signal.connect(self.update_spot)
            self.worker.selected_market_signal.connect(self.update_selected_market)
            self.worker.cash_balance_signal.connect(self.update_cash_balance)
            self.worker.session_pnl_signal.connect(self.update_session_pnl)

            self.loop_running = True
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.worker_thread.start()
            self.set_loop_status("Loop: running")
            self.append_log("Connected!")
        except Exception as exc:
            self.loop_running = False
            self.set_disconnected_state(f"Connection failed: {exc}")

    def stop_bot(self) -> None:
        if not self.loop_running and not self.worker_thread:
            return
        self.append_log("Stop requested by user")
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
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.set_loop_status("Loop: stopped")
        if self.client:
            self.client.close()
            self.client = None
        self.set_disconnected_state()
        self.append_log("Live loop stopped")

    def closeEvent(self, event):  # noqa: N802
        self.stop_bot()
        super().closeEvent(event)
