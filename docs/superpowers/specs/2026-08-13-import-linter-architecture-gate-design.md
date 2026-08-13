# Import Linter Architecture Gate Design

**Date:** 2026-08-13

## Purpose

Prevent new reverse imports and architectural cycles from rebuilding the
coupling removed by the application-domain refactor. The gate must describe
allowed dependency direction declaratively, run with the ordinary hardware-free
test suite, and fail with an import chain that identifies the violation.

## Current State

The project already has `import-linter` 2.12 and Grimp 3.14 in its shared
development environment, but the repository contains no Import Linter
configuration. Existing architecture tests inspect selected modules, class
ownership, and imports. They do not provide a repository-wide dependency graph
contract.

The current import graph contains intentional cross-domain relationships, so a
single strict layered contract would either fail immediately or require broad
exceptions. The first pass therefore protects only dependency directions that
are already unambiguous and required by the project architecture.

## Design

### Configuration

Store Import Linter configuration in `pyproject.toml` under
`[tool.importlinter]` and `[[tool.importlinter.contracts]]`.

Use `probe_station_gui` as the root package, include external import names so
the top-level `main.py` module can be forbidden, and exclude imports guarded
only by `TYPE_CHECKING`. Give every contract a stable identifier so it can be
run alone during diagnosis.

The initial contracts are:

1. **Domain packages do not import the application shell.**
   `api`, `camera`, `coordinates`, `design`, `dialogs`, `instruments`,
   `notifications`, `route`, `settings`, `shared`, `stage`, and `views` must not
   import `probe_station_gui.application`.
2. **Shared stays at the bottom.**
   `probe_station_gui.shared` must not import any of the domain, application, or
   view packages.
3. **Route stays independent of application and GUI composition.**
   `probe_station_gui.route` must not import `application`, `main`, or `views`.
   Existing route imports of narrowly scoped dialogs remain outside this first
   contract and are a later deepening candidate.
4. **API transport stays independent of application and GUI composition.**
   `probe_station_gui.api` must not import `application`, `main`, `dialogs`, or
   `views`. Application composition may import API transport and inject direct
   callbacks; the reverse direction is forbidden.

The contracts check descendants and indirect imports. Do not add blanket
`ignore_imports` entries. If an existing direct or indirect path violates a
proposed contract, narrow the contract to the stable rule or address the
dependency in a separately reviewed refactor pass.

### Dependency

Declare Import Linter in a new `dev` optional dependency group. Keep the
project's advertised Python 3.9 support by selecting `import-linter==1.12.1`
below Python 3.10 and `import-linter==2.12` on Python 3.10 and newer. Both
versions support the TOML forbidden contracts used by this pass. Runtime
dependencies remain unchanged. The shared Python 3.11 environment already has
2.12; the declaration makes a fresh development environment reproducible.

### Permanent Test Gate

Add a small repository-maintainability test that runs Import Linter against the
checked-in `pyproject.toml` with caching disabled. It must:

- use the active interpreter's Import Linter installation;
- run from the repository root;
- fail with Import Linter's contract output;
- avoid importing the GUI, starting threads, or touching hardware/network;
- remain independent of the user's working-directory cache.

The CLI remains directly runnable for diagnosis:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\lint-imports.exe --no-cache
```

### TDD Proof

Before accepting the gate, prove that it detects a real violation:

1. Add the permanent test and configuration.
2. Temporarily introduce one forbidden reverse import in a disposable test
   package/module or inject it into a copied import graph fixture.
3. Run the narrow gate and record the expected broken contract and import chain.
4. Remove the artificial violation.
5. Run the same gate and record success on the real repository graph.

The proof must not leave a forbidden import, generated cache, or test-only
production module in the final tree.

## Error Handling

- Missing Import Linter is a hard development-environment failure with an
  installation hint; it is not silently skipped.
- Invalid configuration is a hard failure.
- A broken contract reports the contract name and import chain supplied by
  Import Linter.
- The gate uses `--no-cache`, so stale graph data cannot hide a violation.

## Verification

The pass is complete when:

- the artificial reverse import produces the intended RED;
- all declared contracts pass on the clean repository graph;
- the permanent pytest gate passes;
- `pyproject.toml` parses and the project remains installable;
- configured Ruff, format, compile, diff, and the repository MI gate pass;
- no Import Linter cache or task-specific temporary path remains;
- an independent review finds no overbroad exception or unprotected reverse
  direction.

## Scope and Sequencing

This pass adds the guardrail only. It does not restructure Route Measurement,
move production behavior, change the GUI, or alter Pause Request, Pause Ack, or
Interrupt semantics.

After the gate is green, the next pass deepens the Route Measurement lifecycle.
Every newly established route module direction will be added to the declarative
contracts as part of that pass rather than deferred to a later cleanup.

## Rejected Alternatives

- **Custom AST import checker:** duplicates graph traversal, indirect-import
  analysis, and diagnostics already provided by Import Linter.
- **Tach or another second architecture tool:** adds a parallel configuration
  model without a capability needed by this pass.
- **A single exhaustive layers contract now:** the current package graph has
  intentional cycles and cross-domain relationships; forcing it into layers
  would produce exception-heavy configuration with little leverage.
- **Pre-commit-only enforcement:** the repository does not currently use
  pre-commit, and local hooks are optional. The pytest gate is part of the
  existing mandatory verification path.
