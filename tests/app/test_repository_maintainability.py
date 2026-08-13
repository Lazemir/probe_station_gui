from __future__ import annotations

import subprocess
from pathlib import Path

from radon.metrics import mi_visit


ROOT = Path(__file__).resolve().parents[2]


def _tracked_python_paths() -> tuple[Path, ...]:
    output = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "*.py"],
        text=True,
    )
    return tuple(ROOT / relative_path for relative_path in output.splitlines())


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
