from pathlib import Path

from scripts import bootstrap, verify_env


def test_bootstrap_uses_project_root(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(bootstrap, "PROJECT_ROOT", tmp_path)
    bootstrap.main()

    assert (tmp_path / "data").exists()
    assert (tmp_path / "logs").exists()

    db_path = tmp_path / "data" / "hourbtc.db"
    assert db_path.exists()


def test_verify_env_uses_project_root(monkeypatch, tmp_path: Path):
    for rel in verify_env.REQUIRED:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    monkeypatch.setattr(verify_env, "PROJECT_ROOT", tmp_path)
    verify_env.main()


def test_setup_launcher_has_root_and_module_execution():
    content = Path("scripts/setup_and_launch.bat").read_text(encoding="utf-8")
    assert 'pushd "%~dp0\\.."' in content
    assert "python -m scripts.bootstrap" in content
    assert "python -m scripts.verify_env" in content
