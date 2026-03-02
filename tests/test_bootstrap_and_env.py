from pathlib import Path

from app.storage.db import DB
from scripts import verify_env


def test_db_bootstrap(tmp_path: Path):
    db = DB(tmp_path / "x.db")
    db.bootstrap()
    rows = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r[0] for r in rows}
    assert {"trades", "events"}.issubset(names)


def test_verify_env_passes(monkeypatch, tmp_path: Path):
    for rel in verify_env.REQUIRED:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    monkeypatch.chdir(tmp_path)
    verify_env.main()
