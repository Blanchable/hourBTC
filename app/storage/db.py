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

    def insert_trade_open(
        self,
        market_ticker: str,
        side: str,
        quantity: int,
        entry_price: float,
        entry_ts: str,
        strategy_version: str,
        diagnostics_entry: dict,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO trades (market_ticker, side, quantity, entry_price, entry_ts, strategy_version, diagnostics_entry)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (market_ticker, side, quantity, entry_price, entry_ts, strategy_version, json.dumps(diagnostics_entry)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        exit_ts: str,
        realized_pnl: float,
        exit_reason: str,
        reached_resolution: bool,
        diagnostics_exit: dict,
    ) -> None:
        self.conn.execute(
            """
            UPDATE trades
            SET exit_price = ?, exit_ts = ?, realized_pnl = ?, exit_reason = ?, reached_resolution = ?, diagnostics_exit = ?
            WHERE id = ?
            """,
            (
                exit_price,
                exit_ts,
                realized_pnl,
                exit_reason,
                1 if reached_resolution else 0,
                json.dumps(diagnostics_exit),
                trade_id,
            ),
        )
        self.conn.commit()

    def fetch_session_trades(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id, market_ticker, side, quantity, entry_price, exit_price, realized_pnl, entry_ts, exit_ts, exit_reason, reached_resolution
            FROM trades
            ORDER BY id DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r[0],
                    "ticker": r[1],
                    "side": r[2],
                    "qty": r[3],
                    "entry_price": r[4],
                    "exit_price": r[5],
                    "realized_pnl": r[6],
                    "entry_ts": r[7],
                    "exit_ts": r[8],
                    "exit_reason": r[9],
                    "resolved": bool(r[10]),
                }
            )
        return out
