from pathlib import Path

from app.storage.db import DB

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    data_dir = PROJECT_ROOT / "data"
    logs_dir = PROJECT_ROOT / "logs"
    data_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)
    DB(data_dir / "hourbtc.db").bootstrap()


if __name__ == "__main__":
    main()
