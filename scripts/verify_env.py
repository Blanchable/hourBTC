from pathlib import Path

REQUIRED = ["requirements.txt", ".env.template", "scripts/bootstrap.py", "app/main.py"]


def main() -> None:
    missing = [p for p in REQUIRED if not Path(p).exists()]
    if missing:
        raise SystemExit(f"Missing required files: {missing}")
    print("Environment verification passed")


if __name__ == "__main__":
    main()
