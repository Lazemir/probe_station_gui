from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from radon.metrics import mi_visit


ROOT = Path(__file__).resolve().parents[2]


def _run_import_linter(
    *,
    config: Path,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    executable_name = "lint-imports.exe" if os.name == "nt" else "lint-imports"
    executable = Path(sys.executable).with_name(executable_name)
    assert executable.is_file(), (
        "Import Linter is missing. Install the development dependencies with "
        "`pip install -e .[dev]`."
    )
    return subprocess.run(
        [str(executable), "--config", str(config), "--no-cache"],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _tracked_python_paths() -> tuple[Path, ...]:
    output = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "*.py"],
        text=True,
    )
    return tuple(ROOT / relative_path for relative_path in output.splitlines())


def test_import_linter_reports_a_forbidden_reverse_import(tmp_path: Path) -> None:
    package = tmp_path / "architecture_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "source.py").write_text(
        "from architecture_fixture import forbidden\n",
        encoding="utf-8",
    )
    (package / "forbidden.py").write_text("", encoding="utf-8")
    config = tmp_path / "pyproject.toml"
    config.write_text(
        dedent(
            """
            [tool.importlinter]
            root_package = "architecture_fixture"

            [[tool.importlinter.contracts]]
            id = "fixture-reverse-import"
            name = "Fixture source does not import forbidden"
            type = "forbidden"
            source_modules = ["architecture_fixture.source"]
            forbidden_modules = ["architecture_fixture.forbidden"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    result = _run_import_linter(config=config, cwd=tmp_path)

    assert result.returncode == 1
    assert "Fixture source does not import forbidden BROKEN" in result.stdout
    assert "architecture_fixture.source" in result.stdout
    assert "architecture_fixture.forbidden" in result.stdout


def test_every_tracked_python_file_has_positive_maintainability_index() -> None:
    nonpositive: dict[str, float] = {}
    for path in _tracked_python_paths():
        maintainability = mi_visit(
            path.read_text(encoding="utf-8-sig"),
            multi=True,
        )
        if maintainability <= 0.0:
            nonpositive[path.relative_to(ROOT).as_posix()] = maintainability

    assert nonpositive == {}
