import json
import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  entry_price REAL NOT NULL,
  exit_price REAL,
  entry_ts TEXT NOT NULL,
  exit_ts TEXT,
  realized_pnl REAL,
  exit_reason TEXT,
  strategy_version TEXT,
  reached_resolution INTEGER DEFAULT 0,
  diagnostics_entry TEXT,
  diagnostics_exit TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  event_type TEXT NOT NULL,
  details TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)

    def bootstrap(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def log_event(self, ts: str, event_type: str, details: dict) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, event_type, details) VALUES (?, ?, ?)",
            (ts, event_type, json.dumps(details)),
        )
        self.conn.commit()
