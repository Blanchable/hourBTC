import logging
import time
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtWidgets import (
    QCheckBox,
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
from app.config.runtime_settings import RuntimeSettingsStore, from_defaults
from app.config.secrets import CredentialProfile, SecretStore
from app.core.controller import Controller
from app.feeds.btc_feed import BTCFeed
from app.feeds.spot_client import SpotClient
from app.runtime.live_worker import LiveWorker
from app.storage.db import DB

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECRETS_PATH = PROJECT_ROOT / "data" / "secrets.json"
DB_PATH = PROJECT_ROOT / "data" / "hourbtc.db"
RUNTIME_SETTINGS_PATH = PROJECT_ROOT / "data" / "runtime_settings.json"


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
        self.settings_store = RuntimeSettingsStore(RUNTIME_SETTINGS_PATH)
        self.runtime_settings = self.settings_store.load(from_defaults(settings.global_settings))

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
        self.mode_label = QLabel("")
        self.trade_series_label = QLabel(f"Trade Series: {settings.global_settings.btc_hourly_trade_series_ticker}")
        self.display_series_label = QLabel(f"Display Series: {settings.global_settings.btc_hourly_range_series_ticker}")
        self.target_family_label = QLabel("Selected Target Family: Threshold")
        self.selected_market_label = QLabel("Selected Market: --")
        self.selected_title_label = QLabel("Selected Title: --")
        self.guardrail_summary_label = QLabel("")

        self.env = QLineEdit("paper")
        self.api_key = QLineEdit()
        self.key_file = QLineEdit()

        self.browse_button = QPushButton("Pick .key")
        self.save_button = QPushButton("Save Credentials")
        self.start_button = QPushButton("Start Bot")
        self.stop_button = QPushButton("Stop Bot")
        self.shadow_button = QPushButton("Shadow Order Test")
        self.smoke_button = QPushButton("Paper Smoke Test")

        self.browse_button.clicked.connect(self.pick_key)
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
        layout.addWidget(self.guardrail_summary_label, 5, 0, 1, 3)

        layout.addWidget(QLabel("Environment (paper/production)"), 6, 0)
        layout.addWidget(self.env, 6, 1)
        layout.addWidget(QLabel("API key ID"), 7, 0)
        layout.addWidget(self.api_key, 7, 1)
        layout.addWidget(QLabel("Private key file"), 8, 0)
        layout.addWidget(self.key_file, 8, 1)
        layout.addWidget(self.browse_button, 8, 2)
        layout.addWidget(self.save_button, 9, 0)
        layout.addWidget(self.start_button, 9, 1)
        layout.addWidget(self.stop_button, 9, 2)
        layout.addWidget(self.shadow_button, 10, 0)
        layout.addWidget(self.smoke_button, 10, 1)

        self.tabs = QTabWidget()
        self.logs = QTextEdit(); self.logs.setReadOnly(True)
        self.series_table = QTableWidget(0, 11)
        self.series_table.setHorizontalHeaderLabels(["Ticker","Title","Close Time","Strike","YES Bid","YES Ask","NO Bid","NO Ask","Mid / Fair Ref","Selected","Last Update"])
        self.active_positions_table = QTableWidget(0, 10)
        self.session_history_table = QTableWidget(0, 10)

        self.settings_tab = QWidget()
        self.settings_layout = QGridLayout(self.settings_tab)
        self._build_settings_tab()

        self.tabs.addTab(self.logs, "Logs")
        self.tabs.addTab(self.series_table, "Series Order Book")
        self.tabs.addTab(self.active_positions_table, "Active Positions")
        self.tabs.addTab(self.session_history_table, "Session History")
        self.tabs.addTab(self.settings_tab, "Settings")
        layout.addWidget(self.tabs, 11, 0, 1, 3)

        self.setCentralWidget(root)
        self._load_credentials_for_env(self.env.text().strip().lower())
        self._load_settings_to_form()
        self._refresh_summary_labels()
        self.append_log("Application started.")

    def _build_settings_tab(self):
        self.s_stop_loss = QLineEdit(); self.s_take_profit = QLineEdit(); self.s_max_pos_contracts = QLineEdit()
        self.s_max_pos_notional = QLineEdit(); self.s_max_total_notional = QLineEdit(); self.s_max_trades = QLineEdit()
        self.s_max_losses = QLineEdit(); self.s_entry_timeout = QLineEdit(); self.s_cooldown = QLineEdit()
        self.s_market_refresh = QLineEdit(); self.s_backoff = QLineEdit(); self.s_fallback_window = QLineEdit(); self.s_quote_stale = QLineEdit()
        self.s_max_api_errors = QLineEdit(); self.s_max_order_rejections = QLineEdit()
        self.s_dry_run = QCheckBox("Dry Run Mode")
        self.s_entry_post_only = QCheckBox("Entry Post-Only")
        self.s_disable_after_stop = QCheckBox("Disable New Entries After Stop")

        rows = [
            ("Session Stop-Loss ($)", self.s_stop_loss), ("Session Take-Profit ($)", self.s_take_profit),
            ("Max Position Contracts", self.s_max_pos_contracts), ("Max Position Notional ($)", self.s_max_pos_notional),
            ("Max Total Open Notional ($)", self.s_max_total_notional), ("Max Trades Per Session", self.s_max_trades),
            ("Max Consecutive Losses", self.s_max_losses), ("Entry Timeout (seconds)", self.s_entry_timeout),
            ("Cooldown After Loss (seconds)", self.s_cooldown), ("Market List Refresh (seconds)", self.s_market_refresh),
            ("Rate Limit Backoff (seconds)", self.s_backoff), ("Fallback Window (hours)", self.s_fallback_window), ("Quote Stale Stop (seconds)", self.s_quote_stale),
            ("Max API Errors Per Session", self.s_max_api_errors), ("Max Order Rejections Per Session", self.s_max_order_rejections),
        ]
        for i, (lbl, w) in enumerate(rows):
            self.settings_layout.addWidget(QLabel(lbl), i, 0); self.settings_layout.addWidget(w, i, 1)
        self.settings_layout.addWidget(self.s_dry_run, len(rows), 0)
        self.settings_layout.addWidget(self.s_entry_post_only, len(rows), 1)
        self.settings_layout.addWidget(self.s_disable_after_stop, len(rows)+1, 0)

        self.btn_save_settings = QPushButton("Save Settings")
        self.btn_apply_settings = QPushButton("Apply to Running Bot")
        self.btn_reset_settings = QPushButton("Reset to Defaults")
        self.btn_disable_entries = QPushButton("Disable New Entries Now")
        self.btn_resume_entries = QPushButton("Resume New Entries")

        self.btn_save_settings.clicked.connect(self.save_runtime_settings)
        self.btn_apply_settings.clicked.connect(self.apply_settings_to_running)
        self.btn_reset_settings.clicked.connect(self.reset_settings_defaults)
        self.btn_disable_entries.clicked.connect(lambda: self._toggle_entries(False))
        self.btn_resume_entries.clicked.connect(lambda: self._toggle_entries(True))

        r = len(rows)+2
        self.settings_layout.addWidget(self.btn_save_settings, r, 0)
        self.settings_layout.addWidget(self.btn_apply_settings, r, 1)
        self.settings_layout.addWidget(self.btn_reset_settings, r+1, 0)
        self.settings_layout.addWidget(self.btn_disable_entries, r+1, 1)
        self.settings_layout.addWidget(self.btn_resume_entries, r+2, 0)

    def _toggle_entries(self, enable: bool):
        if self.worker:
            self.worker.new_entries_disabled = not enable
            self.append_log("New entries resumed" if enable else "New entries disabled")

    def _refresh_summary_labels(self):
        self.mode_label.setText(f"Mode: {'DRY RUN' if self.runtime_settings.dry_run_mode else 'LIVE'}")
        self.guardrail_summary_label.setText(
            f"Guardrails: stop -${self.runtime_settings.session_stop_loss_cents/100:.2f} | take +${self.runtime_settings.session_take_profit_cents/100:.2f} | max pos ${self.runtime_settings.max_position_notional_cents/100:.2f} | max trades {self.runtime_settings.max_trades_per_session} | dry run {'ON' if self.runtime_settings.dry_run_mode else 'OFF'}"
        )

    def _load_settings_to_form(self):
        rs = self.runtime_settings
        self.s_stop_loss.setText(f"{rs.session_stop_loss_cents/100:.2f}")
        self.s_take_profit.setText(f"{rs.session_take_profit_cents/100:.2f}")
        self.s_max_pos_contracts.setText(str(rs.max_position_contracts))
        self.s_max_pos_notional.setText(f"{rs.max_position_notional_cents/100:.2f}")
        self.s_max_total_notional.setText(f"{rs.max_total_open_notional_cents/100:.2f}")
        self.s_max_trades.setText(str(rs.max_trades_per_session))
        self.s_max_losses.setText(str(rs.max_consecutive_losses))
        self.s_entry_timeout.setText(str(rs.entry_order_timeout_seconds))
        self.s_cooldown.setText(str(rs.cooldown_after_loss_seconds))
        self.s_market_refresh.setText(str(rs.market_list_refresh_seconds))
        self.s_backoff.setText(str(rs.rate_limit_backoff_seconds))
        self.s_fallback_window.setText(str(rs.fallback_window_hours))
        self.s_quote_stale.setText(str(rs.quote_stale_stop_seconds))
        self.s_max_api_errors.setText(str(rs.max_api_errors_per_session))
        self.s_max_order_rejections.setText(str(rs.max_order_rejections_per_session))
        self.s_dry_run.setChecked(rs.dry_run_mode)
        self.s_entry_post_only.setChecked(rs.entry_post_only)
        self.s_disable_after_stop.setChecked(rs.disable_new_entries_after_stop_hit)

    def _read_settings_from_form(self):
        try:
            rs = type(self.runtime_settings)(
                session_stop_loss_cents=int(float(self.s_stop_loss.text()) * 100),
                session_take_profit_cents=int(float(self.s_take_profit.text()) * 100),
                max_position_contracts=int(self.s_max_pos_contracts.text()),
                max_position_notional_cents=int(float(self.s_max_pos_notional.text()) * 100),
                max_total_open_notional_cents=int(float(self.s_max_total_notional.text()) * 100),
                max_trades_per_session=int(self.s_max_trades.text()),
                max_consecutive_losses=int(self.s_max_losses.text()),
                entry_order_timeout_seconds=int(self.s_entry_timeout.text()),
                cooldown_after_loss_seconds=int(self.s_cooldown.text()),
                market_list_refresh_seconds=int(self.s_market_refresh.text()),
                rate_limit_backoff_seconds=int(self.s_backoff.text()),
                rate_limit_backoff_max_seconds=max(int(self.s_backoff.text()) * 3, int(self.s_backoff.text())),
                fallback_window_hours=int(self.s_fallback_window.text()),
                dry_run_mode=self.s_dry_run.isChecked(),
                entry_post_only=self.s_entry_post_only.isChecked(),
                disable_new_entries_after_stop_hit=self.s_disable_after_stop.isChecked(),
                quote_stale_stop_seconds=int(self.s_quote_stale.text()),
                max_api_errors_per_session=int(self.s_max_api_errors.text()),
                max_order_rejections_per_session=int(self.s_max_order_rejections.text()),
            )
        except Exception:
            self.append_error("Invalid settings values")
            return None
        errs = []
        if rs.max_position_contracts < 1: errs.append("Max Position Contracts must be >= 1")
        if rs.max_trades_per_session < 1: errs.append("Max Trades must be >= 1")
        if rs.entry_order_timeout_seconds < 1: errs.append("Entry Timeout must be >= 1")
        if rs.market_list_refresh_seconds < 5: errs.append("Market List Refresh must be >= 5")
        if rs.rate_limit_backoff_seconds < 1: errs.append("Rate limit backoff must be >= 1")
        if rs.fallback_window_hours < 1: errs.append("Fallback window must be >= 1")
        if rs.max_total_open_notional_cents < rs.max_position_notional_cents: errs.append("Max total notional must be >= max position notional")
        if rs.session_stop_loss_cents < 0 or rs.session_take_profit_cents < 0: errs.append("Dollar values must be non-negative")
        if errs:
            self.append_error("; ".join(errs))
            return None
        return rs

    def save_runtime_settings(self):
        rs = self._read_settings_from_form()
        if not rs:
            return
        self.runtime_settings = rs
        self.settings_store.save(rs)
        self._refresh_summary_labels()
        self.append_log("Settings saved")

    def apply_settings_to_running(self):
        rs = self._read_settings_from_form()
        if not rs:
            return
        self.runtime_settings = rs
        self.settings_store.save(rs)
        self._refresh_summary_labels()
        if self.worker:
            self.worker.apply_runtime_settings(rs)
            self.append_log("Settings applied to running bot")
        else:
            self.append_log("Bot is not running. Settings saved but not applied live.")

    def reset_settings_defaults(self):
        self.runtime_settings = from_defaults(settings.global_settings)
        self._load_settings_to_form()
        self._refresh_summary_labels()
        self.append_log("Settings reset to defaults")

    def append_log(self, m: str): self.logs.append(m); self.logger.info(m)
    def append_error(self, m: str): self.logs.append(f"ERROR: {m}"); self.logger.error(m)
    def pick_key(self):
        f,_=QFileDialog.getOpenFileName(self,"Select private key","","Key Files (*.key *.pem)")
        if f: self.key_file.setText(f)
    def _normalize_env(self): return self.env.text().strip().lower()
    def _load_credentials_for_env(self, env: str):
        if env in {"paper","production"}:
            p=self.secret_store.load(env); self.api_key.setText(p.api_key_id); self.key_file.setText(p.private_key_path)
    def save_credentials(self):
        env=self._normalize_env()
        if env not in {"paper","production"}: self.append_error("environment must be paper/production"); return
        self.secret_store.save(env, CredentialProfile(api_key_id=self.api_key.text().strip(), private_key_path=self.key_file.text().strip())); self.append_log(f"Saved credentials for {env}")
    def _extract_available_balance_cents(self,s):
        try: return int(float(s.get("balance")))
        except Exception: return None
    def _validate_inputs(self):
        env=self._normalize_env(); a=self.api_key.text().strip(); k=self.key_file.text().strip()
        if env not in {"paper","production"} or not a or not k or not Path(k).exists(): self.append_error("Missing/invalid credentials input"); return None
        return env,a,k
    def _format_cents_as_dollars(self,c): return f"${float(c)/100:.2f}"
    def update_spot(self,p): self.spot_label.setText(f"BTC Spot: ${p:,.2f}")
    def update_spot_meta(self, ts, age): self.last_spot_update_label.setText(f"Last Spot Update: {ts}"); self.feed_age_label.setText(f"Feed Age: {age:.1f}s")
    def update_selected_market(self,t): self.selected_market_label.setText(f"Selected Market: {t or '--'}")
    def update_selected_title(self,t): self.selected_title_label.setText(f"Selected Title: {t or '--'}")
    def update_cash_balance(self,c): self.cash_balance_label.setText(f"Cash Available: {self._format_cents_as_dollars(c)}")
    def update_session_pnl(self,c): self.session_pnl_label.setText(f"Session PnL: {'-' if c<0 else ''}{self._format_cents_as_dollars(abs(c))}")
    def set_loop_status(self,t): self.loop_status_label.setText(t)

    def update_series_orderbook(self, rows):
        self.series_table.setRowCount(len(rows))
        for i,r in enumerate(rows):
            mid=((r["yes_bid"]+r["yes_ask"])/2) if r.get("yes_bid") is not None and r.get("yes_ask") is not None else None
            vals=[r.get("ticker",""),r.get("title",""),r.get("close_time",""),r.get("strike","--") if r.get("strike") is not None else "--",r.get("yes_bid","--") if r.get("yes_bid") is not None else "--",r.get("yes_ask","--") if r.get("yes_ask") is not None else "--",r.get("no_bid","--") if r.get("no_bid") is not None else "--",r.get("no_ask","--") if r.get("no_ask") is not None else "--",f"{mid:.2f}" if mid is not None else "--",r.get("selected",""),r.get("last_update","")]
            for c,v in enumerate(vals): self.series_table.setItem(i,c,QTableWidgetItem(str(v)))

    def start_bot(self):
        if self.loop_running: return
        v=self._validate_inputs()
        if not v: return
        env,a,k=v; self.save_credentials()
        self.client=KalshiClient(env,a,k)
        bal=self._extract_available_balance_cents(self.client.get_account_summary())
        if bal is None: self.append_error("Could not read balance"); return
        self.controller=Controller(self.client,BTCFeed()); self.controller.connect()
        self.worker=LiveWorker(self.client,self.controller,bal,self.db,spot_client=SpotClient(),runtime_settings=self.runtime_settings)
        self.worker_thread=QThread(self); self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.start)
        self.worker.finished_signal.connect(self.worker_thread.quit)
        self.worker.log_signal.connect(self.append_log); self.worker.error_signal.connect(self.append_error)
        self.worker.loop_status_signal.connect(self.set_loop_status); self.worker.series_rows_signal.connect(self.update_series_orderbook)
        self.worker.selected_market_signal.connect(self.update_selected_market); self.worker.selected_title_signal.connect(self.update_selected_title)
        self.worker.spot_signal.connect(self.update_spot); self.worker.spot_meta_signal.connect(self.update_spot_meta)
        self.worker.cash_balance_signal.connect(self.update_cash_balance); self.worker.session_pnl_signal.connect(self.update_session_pnl)
        self.loop_running=True; self.connection_status_label.setText("Connected!"); self.start_button.setEnabled(False); self.stop_button.setEnabled(True)
        self.worker_thread.start()

    def stop_bot(self):
        if self.worker: self.worker.stop()
        if self.worker_thread: self.worker_thread.wait(6000)
        if self.worker: self.worker.shutdown_cleanup()
        if self.client: self.client.close(); self.client=None
        self.worker=None; self.worker_thread=None; self.loop_running=False
        self.connection_status_label.setText("Not connected")
        self.start_button.setEnabled(True); self.stop_button.setEnabled(False)

    def shadow_order_test(self):
        self.append_log("Shadow test started")
        if self.loop_running and self.worker:
            ctx=self.worker.get_cached_shadow_context()
            if not ctx.get("target"):
                self.append_log("Shadow test unavailable while live loop is refreshing. Stop the loop or use cached mode.")
                return
            self.append_log("Using cached shadow mode while live loop is running")
            self.append_log(f"Trade series: {settings.global_settings.btc_hourly_trade_series_ticker}")
            t=ctx["target"]; self.append_log(f"Selected threshold target: {t.get('ticker')}")
            self.append_log(f"Target title: {t.get('title')}")
            self.append_log(f"Spot: {ctx.get('spot')}")
            self.append_log(f"Spot age: {ctx.get('spot_age'):.1f}s")
            r=next((x for x in ctx.get("rows",[]) if x.get("ticker")==t.get("ticker")),None)
            if r:
                self.append_log(f"Orderbook: yes_bid={r.get('yes_bid')} yes_ask={r.get('yes_ask')} no_bid={r.get('no_bid')} no_ask={r.get('no_ask')}")
            decision=self.controller.evaluate({"yes_bid":r.get("yes_bid") or 0,"yes_ask":r.get("yes_ask") or 0,"no_bid":r.get("no_bid") or 0,"no_ask":r.get("no_ask") or 0},1200) if self.controller and r else "no_trade"
            self.append_log(f"Strategy decision: {decision}")
            payload={"ticker":t.get("ticker"),"side":"yes" if decision=="buy_yes" else "no","action":"buy","count":1,"limit_price":int((r.get('yes_ask') if decision=="buy_yes" else r.get('no_ask')) or 1)} if r else {}
            self.append_log(f"Would submit order: {payload}")
            return

        v=self._validate_inputs();
        if not v: return
        env,a,k=v; c=KalshiClient(env,a,k)
        try:
            self.append_log(f"Trade series: {settings.global_settings.btc_hourly_trade_series_ticker}")
            spot=SpotClient().fetch_btc_spot(); t=c.resolve_btc_hourly_trade_target_market(spot_price=spot, markets=c.get_live_candidate_trade_markets(force_refresh=True, fallback_window_hours=self.runtime_settings.fallback_window_hours, cache_ttl_seconds=self.runtime_settings.market_list_refresh_seconds))
            if not t: self.append_log("No threshold target found"); return
            self.append_log(f"Selected threshold target: {t['ticker']}"); self.append_log(f"Target title: {t.get('title')}")
            rows=c.get_hourly_series_quote_rows([t]); r=rows[0]
            self.append_log(f"Spot: {spot}"); self.append_log("Spot age: 0.0s")
            self.append_log(f"Orderbook: yes_bid={r.get('yes_bid')} yes_ask={r.get('yes_ask')} no_bid={r.get('no_bid')} no_ask={r.get('no_ask')}")
            decision="no_trade"; self.append_log(f"Strategy decision: {decision}")
            payload={"ticker":t.get("ticker"),"side":"yes","action":"buy","count":1,"limit_price":int(r.get("yes_ask") or 1)}
            self.append_log(f"Would submit order: {payload}")
        finally:
            c.close()

    def paper_smoke_test(self):
        if self._normalize_env() != "paper": self.append_log("Paper Smoke Test is disabled in production."); return
        self.append_log("Paper smoke test started")
        v=self._validate_inputs();
        if not v: return
        env,a,k=v; c=KalshiClient(env,a,k)
        try:
            spot=SpotClient().fetch_btc_spot(); t=c.resolve_btc_hourly_trade_target_market(spot_price=spot, markets=c.get_live_candidate_trade_markets(force_refresh=True, fallback_window_hours=self.runtime_settings.fallback_window_hours, cache_ttl_seconds=self.runtime_settings.market_list_refresh_seconds))
            if not t: self.append_error("No threshold target found for smoke test"); return
            r=c.get_hourly_series_quote_rows([t])[0]; yes_bid=int(r.get("yes_bid") or 20); safe=max(1, yes_bid-20)
            self.append_log(f"Selected threshold target: {t['ticker']}")
            self.append_log(f"Submitting safe resting test order: side=yes price={safe}")
            resp=c.place_entry_order(ticker=t["ticker"],side="yes",count=1,limit_price=safe,client_order_id=f"smoke-{int(time.time())}",post_only=True,time_in_force="good_till_canceled")
            o=resp.get("order",resp); oid=o.get("order_id")
            self.append_log(f"Smoke test order accepted: order_id={oid} status={o.get('status')}")
            c.cancel_order(str(oid)); self.append_log("Smoke test order canceled successfully")
        except Exception as exc:
            self.append_error(str(exc))
        finally:
            c.close()

    def closeEvent(self, event):
        self.stop_bot(); super().closeEvent(event)
