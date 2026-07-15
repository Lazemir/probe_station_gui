from pathlib import Path
import tomllib


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_klayout_is_the_only_production_gds_dependency() -> None:
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text("utf-8"))
    dependencies = project["project"]["dependencies"]

    assert "klayout==0.30.9" in dependencies
    assert all(not dependency.lower().startswith("gdstk") for dependency in dependencies)


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
