from pathlib import Path
import tomllib


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_klayout_is_the_only_production_gds_dependency() -> None:
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text("utf-8"))
    dependencies = project["project"]["dependencies"]

    assert "klayout==0.30.9" in dependencies
    assert all(not dependency.lower().startswith("gdstk") for dependency in dependencies)


def test_windows_bootstrap_lock_installs_the_production_klayout_version() -> None:
    lock_path = REPOSITORY_ROOT / "requirements-windows-py311.lock"
    bootstrap_path = REPOSITORY_ROOT / "scripts" / "bootstrap_venv.cmd"
    locked_dependencies = {
        line.strip().lower()
        for line in lock_path.read_text("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    bootstrap = bootstrap_path.read_text("utf-8").lower()

    assert "klayout==0.30.9" in locked_dependencies
    assert all(not dependency.startswith("gdstk") for dependency in locked_dependencies)
    assert "pip install -r requirements-windows-py311.lock" in bootstrap


def test_throwaway_klayout_prototype_is_not_packaged() -> None:
    assert not (REPOSITORY_ROOT / "probe_station_gui" / "prototypes").exists()


def test_production_sources_have_no_obsolete_gds_runtime_debris() -> None:
    source = "\n".join(
        path.read_text("utf-8")
        for path in (REPOSITORY_ROOT / "probe_station_gui").rglob("*.py")
    )

    assert "gdstk" not in source
    assert "DESIGN SNAP geometry ready" not in source
    assert "MINIMAP RENDER simplified" not in source
    assert "MINIMAP KLAYOUT" not in source

    executable_configuration = "\n".join(
        (
            (REPOSITORY_ROOT / "pyproject.toml").read_text("utf-8"),
            (REPOSITORY_ROOT / "requirements-windows-py311.lock").read_text("utf-8"),
            (REPOSITORY_ROOT / "scripts" / "bootstrap_venv.cmd").read_text("utf-8"),
        )
    ).lower()
    assert "gdstk" not in executable_configuration
