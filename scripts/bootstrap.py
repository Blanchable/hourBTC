from pathlib import Path

from app.storage.db import DB


def main() -> None:
    Path("data").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)
    DB(Path("data/hourbtc.db")).bootstrap()


if __name__ == "__main__":
    main()
