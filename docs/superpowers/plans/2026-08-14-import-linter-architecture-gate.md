# Import Linter Architecture Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a permanent Import Linter gate that prevents new reverse imports into the application shell and GUI composition packages.

**Architecture:** Keep dependency rules declarative in `pyproject.toml` and invoke the installed Import Linter CLI from the existing repository-maintainability pytest module. Characterize a deliberately broken disposable package first, then make the real repository test fail before adding the four approved contracts.

**Tech Stack:** Python 3.9+, pytest, Import Linter 1.12.1/2.12, Grimp, TOML, Ruff

## Global Constraints

- Runtime dependencies remain unchanged; Import Linter belongs only to the `dev` optional dependency group.
- Python 3.9 selects `import-linter==1.12.1`; Python 3.10 and newer select `import-linter==2.12`.
- Use the shared project environment at `C:\Users\Public\code\probe_station_gui\.venv` for every Python command.
- Import Linter runs with `--no-cache`; no `.import_linter_cache` may remain.
- The gate must not import the GUI, start threads, access hardware, or use the network.
- No blanket `ignore_imports` entries are allowed.
- This pass does not change Route Measurement, Pause Request, Pause Ack, or Interrupt behavior.

---

## File Structure

- `pyproject.toml`: declares the development dependency and owns the four Import Linter contracts.
- `tests/app/test_repository_maintainability.py`: owns both repository-wide maintainability gates and the CLI characterization that proves forbidden imports are detected.
- `docs/superpowers/specs/2026-08-13-import-linter-architecture-gate-design.md`: approved design; implementation does not broaden it.
- `docs/superpowers/plans/2026-08-14-import-linter-architecture-gate.md`: this execution record.

### Task 1: Characterize the Import Linter failure path

**Files:**
- Modify: `pyproject.toml`
- Modify: `tests/app/test_repository_maintainability.py`

**Interfaces:**
- Consumes: the `lint-imports` console script installed beside `sys.executable`.
- Produces: `_run_import_linter(*, config: Path, cwd: Path) -> subprocess.CompletedProcess[str]`, used by Task 2.

- [ ] **Step 1: Add the Import Linter development dependency**

Append this optional dependency group after the existing `lcr` group:

```toml
dev = [
    "import-linter==1.12.1; python_version < '3.10'",
    "import-linter==2.12; python_version >= '3.10'",
]
```

- [ ] **Step 2: Add the CLI runner and disposable broken-graph characterization**

Extend `tests/app/test_repository_maintainability.py` with these imports:

```python
import os
import sys
from textwrap import dedent
```

Add this helper below `ROOT`:

```python
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
```

Add this test before the repository MI test:

```python
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
```

- [ ] **Step 3: Run the characterization and observe the deliberate broken contract**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_repository_maintainability.py::test_import_linter_reports_a_forbidden_reverse_import -q -p no:cacheprovider --basetemp=.pytest-import-linter-characterization
```

Expected: `1 passed`. The captured Import Linter process must return `1`, and the test must prove its output names the broken contract and both modules.

- [ ] **Step 4: Verify metadata parses and no cache was created**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -c "import tomllib; tomllib.load(open('pyproject.toml', 'rb')); print('pyproject ok')"
Test-Path .import_linter_cache
```

Expected: `pyproject ok`, then `False`.

- [ ] **Step 5: Commit the characterized tool seam**

```powershell
git add pyproject.toml tests/app/test_repository_maintainability.py
git commit -m "test: characterize import architecture violations"
```

### Task 2: Enforce the repository import contracts

**Files:**
- Modify: `pyproject.toml`
- Modify: `tests/app/test_repository_maintainability.py`

**Interfaces:**
- Consumes: `_run_import_linter(*, config: Path, cwd: Path) -> subprocess.CompletedProcess[str]` from Task 1.
- Produces: four stable contract identifiers: `domain-no-application`, `shared-bottom`, `route-no-gui-composition`, and `api-no-gui-composition`.

- [ ] **Step 1: Write the failing repository gate**

Add this test after the disposable characterization:

```python
def test_repository_import_contracts_pass() -> None:
    result = _run_import_linter(config=ROOT / "pyproject.toml", cwd=ROOT)

    assert result.returncode == 0, result.stdout
    assert "Contracts: 4 kept, 0 broken." in result.stdout
```

- [ ] **Step 2: Run the gate to verify RED**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_repository_maintainability.py::test_repository_import_contracts_pass -q -p no:cacheprovider --basetemp=.pytest-import-linter-red
```

Expected: FAIL because `pyproject.toml` has no `[tool.importlinter]` configuration.

- [ ] **Step 3: Add the four minimal contracts**

Append this exact configuration to `pyproject.toml`:

```toml
[tool.importlinter]
root_package = "probe_station_gui"
include_external_packages = true
exclude_type_checking_imports = true

[[tool.importlinter.contracts]]
id = "domain-no-application"
name = "Domain packages do not import the application shell"
type = "forbidden"
source_modules = [
    "probe_station_gui.api",
    "probe_station_gui.camera",
    "probe_station_gui.coordinates",
    "probe_station_gui.design",
    "probe_station_gui.dialogs",
    "probe_station_gui.instruments",
    "probe_station_gui.notifications",
    "probe_station_gui.route",
    "probe_station_gui.settings",
    "probe_station_gui.shared",
    "probe_station_gui.stage",
    "probe_station_gui.views",
]
forbidden_modules = ["probe_station_gui.application"]

[[tool.importlinter.contracts]]
id = "shared-bottom"
name = "Shared stays below application and domain packages"
type = "forbidden"
source_modules = ["probe_station_gui.shared"]
forbidden_modules = [
    "main",
    "probe_station_gui.api",
    "probe_station_gui.application",
    "probe_station_gui.camera",
    "probe_station_gui.coordinates",
    "probe_station_gui.design",
    "probe_station_gui.dialogs",
    "probe_station_gui.instruments",
    "probe_station_gui.notifications",
    "probe_station_gui.route",
    "probe_station_gui.settings",
    "probe_station_gui.stage",
    "probe_station_gui.views",
]

[[tool.importlinter.contracts]]
id = "route-no-gui-composition"
name = "Route stays independent of application and GUI composition"
type = "forbidden"
source_modules = ["probe_station_gui.route"]
forbidden_modules = [
    "main",
    "probe_station_gui.application",
    "probe_station_gui.views",
]

[[tool.importlinter.contracts]]
id = "api-no-gui-composition"
name = "API transport stays independent of application and GUI composition"
type = "forbidden"
source_modules = ["probe_station_gui.api"]
forbidden_modules = [
    "main",
    "probe_station_gui.application",
    "probe_station_gui.dialogs",
    "probe_station_gui.views",
]
```

- [ ] **Step 4: Run the same gate to verify GREEN**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_repository_maintainability.py::test_repository_import_contracts_pass -q -p no:cacheprovider --basetemp=.pytest-import-linter-green
```

Expected: `1 passed` and captured Import Linter summary `Contracts: 4 kept, 0 broken.`

- [ ] **Step 5: Run each contract directly by stable identifier**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\lint-imports.exe --no-cache --contract domain-no-application --contract shared-bottom --contract route-no-gui-composition --contract api-no-gui-composition
```

Expected: `Contracts: 4 kept, 0 broken.`

- [ ] **Step 6: Run the complete maintainability module**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_repository_maintainability.py -q -p no:cacheprovider --basetemp=.pytest-import-linter-maintainability
```

Expected: `3 passed`: detector characterization, repository import contracts, and every tracked Python file MI above zero.

- [ ] **Step 7: Run scoped static verification**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check tests/app/test_repository_maintainability.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff format --check tests/app/test_repository_maintainability.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m compileall -q tests/app/test_repository_maintainability.py
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 8: Remove exact task temporary directories and verify cache absence**

Remove only these paths if pytest created them:

```text
.pytest-import-linter-characterization
.pytest-import-linter-red
.pytest-import-linter-green
.pytest-import-linter-maintainability
```

Then run:

```powershell
Test-Path .import_linter_cache
git status --short
```

Expected: cache `False`; status contains only `pyproject.toml`, the maintainability test, and this plan's factual checkbox updates.

- [ ] **Step 9: Commit the repository contracts**

```powershell
git add pyproject.toml tests/app/test_repository_maintainability.py docs/superpowers/plans/2026-08-14-import-linter-architecture-gate.md
git commit -m "build: enforce import architecture contracts"
```

### Task 3: Independent review and handoff to Route deepening

**Files:**
- Modify: `docs/superpowers/plans/2026-08-14-import-linter-architecture-gate.md`

**Interfaces:**
- Consumes: the two Task 1-2 commits and four stable Import Linter contracts.
- Produces: a clean reviewed branch ready for the Route Measurement lifecycle design pass.

- [ ] **Step 1: Review contract precision**

Use the requesting-code-review workflow. The reviewer must independently verify:

- every declared source and forbidden module exists in the graph or is the intentional external `main` module;
- all four contracts fail on a representative injected reverse import;
- no `ignore_imports`, optional module, or direct-only switch weakens the rules;
- no runtime dependency was added;
- Python 3.9 and 3.10+ markers are mutually exclusive and exhaustive;
- the test invokes the environment-local executable and prints actionable failure output.

- [ ] **Step 2: Re-run the frozen gate after review fixes**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_repository_maintainability.py -q -p no:cacheprovider --basetemp=.pytest-import-linter-final
C:\Users\Public\code\probe_station_gui\.venv\Scripts\lint-imports.exe --no-cache
git diff --check
```

Expected: `3 passed`, `4 kept / 0 broken`, and clean diff check.

- [ ] **Step 3: Record final evidence and commit review facts**

Update this plan's checkboxes with exact counts, reviewer verdict, and commit IDs. Remove `.pytest-import-linter-final`, verify `.import_linter_cache` is absent, then commit only the factual plan update:

```powershell
git add docs/superpowers/plans/2026-08-14-import-linter-architecture-gate.md
git commit -m "docs: record import architecture gate evidence"
```

- [ ] **Step 4: Begin the next approved architecture candidate**

Return to the Route Measurement lifecycle model. Treat the four contracts as a mandatory gate for every subsequent refactor pass, and add any newly established route direction to `pyproject.toml` in the same commit that creates it.
