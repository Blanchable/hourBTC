import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QWidget,
)

from app.brokers.kalshi_client import KalshiClient
from app.config.secrets import CredentialProfile, SecretStore
from app.core.controller import Controller
from app.feeds.btc_feed import BTCFeed

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECRETS_PATH = PROJECT_ROOT / "data" / "secrets.json"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.client = None
        self.controller = None

        self.secret_store = SecretStore(SECRETS_PATH)

        self.setWindowTitle("hourBTC Kalshi 1H Bot")
        root = QWidget()
        layout = QGridLayout(root)

        self.connection_status_label = QLabel("Not connected")
        self.connection_status_label.setStyleSheet("color: #b00020; font-weight: bold;")
        self.cash_balance_label = QLabel("Cash Available: --")

        self.env = QLineEdit("paper")
        self.api_key = QLineEdit()
        self.key_file = QLineEdit()
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)

        self.browse_button = QPushButton("Pick .key")
        self.browse_button.clicked.connect(self.pick_key)

        self.save_button = QPushButton("Save Credentials")
        self.start_button = QPushButton("Start Bot")
        self.stop_button = QPushButton("Stop Bot")

        self.save_button.clicked.connect(self.save_credentials)
        self.start_button.clicked.connect(self.start_bot)
        self.stop_button.clicked.connect(self.stop_bot)

        layout.addWidget(self.connection_status_label, 0, 0, 1, 3)
        layout.addWidget(self.cash_balance_label, 1, 0, 1, 3)
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
        layout.addWidget(self.logs, 6, 0, 1, 3)

        self.setCentralWidget(root)
        self.set_disconnected_state()
        self.log_message("Application started.")
        self._load_credentials_for_env(self.env.text().strip().lower())

    def pick_key(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "Select private key", "", "Key Files (*.key *.pem)")
        if file_name:
            self.key_file.setText(file_name)

    def log_message(self, message: str) -> None:
        self.logs.append(message)
        self.logger.info(message)

    def log_error(self, message: str) -> None:
        self.logs.append(f"ERROR: {message}")
        self.logger.error(message)

    def set_connected_state(self, cash_display: str) -> None:
        self.connection_status_label.setText("Connected!")
        self.connection_status_label.setStyleSheet("color: #117a37; font-weight: bold;")
        self.cash_balance_label.setText(f"Cash Available: {cash_display}")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def set_disconnected_state(self, reason: str | None = None) -> None:
        self.connection_status_label.setText("Not connected")
        self.connection_status_label.setStyleSheet("color: #b00020; font-weight: bold;")
        self.cash_balance_label.setText("Cash Available: --")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if reason:
            self.log_error(reason)

    def _normalize_env(self) -> str:
        return self.env.text().strip().lower()

    def _load_credentials_for_env(self, env: str) -> None:
        if env not in {"paper", "production"}:
            return
        profile = self.secret_store.load(env)
        self.api_key.setText(profile.api_key_id)
        self.key_file.setText(profile.private_key_path)
        if profile.api_key_id or profile.private_key_path:
            self.log_message(f"Loaded saved credentials for {env}")

    def _validate_inputs(self, env: str, api_key_id: str, key_file: str) -> bool:
        if env not in {"paper", "production"}:
            self.log_error("Environment must be 'paper' or 'production'.")
            return False
        if not api_key_id:
            self.log_error("API key ID is required.")
            return False
        if not key_file:
            self.log_error("Private key path is required.")
            return False
        if not Path(key_file).exists():
            self.log_error(f"Private key file not found: {key_file}")
            return False
        return True

    def save_credentials(self) -> None:
        env = self._normalize_env()
        if env not in {"paper", "production"}:
            self.log_error("Cannot save credentials: environment must be paper or production.")
            return
        profile = CredentialProfile(
            api_key_id=self.api_key.text().strip(),
            private_key_path=self.key_file.text().strip(),
        )
        self.secret_store.save(env, profile)
        self.log_message(f"Saved credentials for {env}")

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
        env = self._normalize_env()
        if env in {"paper", "production"}:
            self._load_credentials_for_env(env)
        api_key_id = self.api_key.text().strip()
        key_file = self.key_file.text().strip()

        if not self._validate_inputs(env, api_key_id, key_file):
            return

        self.save_credentials()

        try:
            client = KalshiClient(env, api_key_id, key_file)
            summary = client.get_account_summary()
            balance_cents = self._extract_available_balance_cents(summary)
            cash_display = self._format_cents_as_dollars(balance_cents or 0)

            self.client = client
            self.controller = Controller(client, BTCFeed())
            self.controller.connect()
            self.controller.refresh_market()

            self.set_connected_state(cash_display)
            self.log_message("Connected!")
            self.log_message(f"Available cash: {cash_display}")
            if self.controller.state.market_ticker:
                self.log_message(f"Active market: {self.controller.state.market_ticker}")
            self.log_message("Connection verified. Bot is ready.")
        except Exception as exc:
            self.client = None
            self.controller = None
            self.set_disconnected_state(f"Connection failed: {exc}")

    def stop_bot(self) -> None:
        self.client = None
        self.controller = None
        self.set_disconnected_state("Bot stopped by user")
        self.log_message("Bot stopped")
