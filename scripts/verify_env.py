from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ["requirements.txt", ".env.template", "scripts/bootstrap.py", "app/main.py"]


def main() -> None:
    missing = [p for p in REQUIRED if not (PROJECT_ROOT / p).exists()]
    if missing:
        raise SystemExit(f"Missing required files (relative to project root): {missing}")
    print("Environment verification passed")


if __name__ == "__main__":
    main()
